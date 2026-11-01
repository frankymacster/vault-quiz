#!/usr/bin/env python3
"""Fit the review schedule to your own answers, and say whether it can be trusted yet.

    python3 fit_scheduler.py <vault> <game-sessions-dir>

Reads every first answer to a kanji's meaning (the CLI quiz log and the game's
sessions), turns each into a review (days since your previous answer, correct
answers in a row before it) and checks how well the schedule's steps keep your
recall near the target. It writes .scheduler.json in the vault:

  stages       recall at the steps that follow the 2nd, 3rd and later successes,
               counting only reviews whose gap is close to that step's interval
  multipliers  how much to lengthen (>1) or shorten (<1) each step, shrunk toward
               1 by how few reviews back it, and limited to 0.85-1.25
  ready        true only when every step has enough reviews behind it
  enabled      whether quiz.py and the game use the multipliers (kept as you set
               it; false until you turn it on)

Your recall depends on how many answers in a row you've got right, but the data
barely reaches past two weeks, so a longer step can't be shown safe until it does.
"""
import collections, datetime, glob, json, math, os, random, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import quiz as q

TARGET = 0.90
STEPS = {"2": (4, 9), "3": (10, 22), "4": (23, 10 ** 6)}      # review gap (days) that tests each step: 6 d, ~15 d, later
NEED = 100                                                    # reviews a step needs before it is trusted
KAPPA, SHRINK, LO, HI = 0.35, 150, 0.85, 1.25


def history(vault, sessions):
    q.use_vault(vault)
    kanji, _ = q.build()
    rows = []
    path = os.path.join(q.VAULT, ".quiz_log.jsonl")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            r = json.loads(line)
            if r.get("outcome") in ("correct", "gave_up") and r.get("char") in kanji:
                ts = r["ts"] if isinstance(r["ts"], (int, float)) else datetime.datetime.fromisoformat(r["ts"]).timestamp()
                rows.append((ts, r["char"], r["outcome"] == "correct"))
    for f in glob.glob(os.path.join(sessions, "*.json")):
        d = json.load(open(f, encoding="utf-8"))
        for e in d.get("data", d).get("events", []):
            if e.get("step") == "meaning" and e.get("k") in kanji and "ok" in e and not e.get("retry"):
                rows.append((e["t"], e["k"], bool(e["ok"])))
    rows.sort()
    seq = collections.defaultdict(dict)
    for ts, k, ok in rows:
        seq[k].setdefault(datetime.date.fromtimestamp(ts).toordinal(), ok)
    reviews = []                                   # (kanji, gap days, correct in a row before, lapses before, correct)
    for k, days in seq.items():
        reps = lapses = 0
        ds = sorted(days)
        for i, d in enumerate(ds):
            if i:
                reviews.append((k, d - ds[i - 1], reps, lapses, days[d]))
            if days[d]:
                reps += 1
            else:
                reps, lapses = 0, lapses + 1
    return reviews, len(seq)


def logit(p):
    return math.log(p / (1 - p))


def fit_logistic(data, cols, l2=1.0, iters=40):
    f = lambda r: [{"b": 1.0, "lnt": math.log(r[1]), "reps": min(r[2], 6), "lapses": min(r[3], 4)}[c] for c in cols]
    n, w = len(cols), [0.0] * len(cols)
    X, y = [f(r) for r in data], [1.0 if r[4] else 0.0 for r in data]
    for _ in range(iters):
        g, H = [0.0] * n, [[0.0] * n for _ in range(n)]
        for x, yy in zip(X, y):
            p = 1 / (1 + math.exp(-sum(a * b for a, b in zip(w, x))))
            for i in range(n):
                g[i] += (p - yy) * x[i]
                for j in range(n):
                    H[i][j] += p * (1 - p) * x[i] * x[j]
        for i in range(1, n):
            g[i] += l2 * w[i]
            H[i][i] += l2
        A = [row[:] + [g[i]] for i, row in enumerate(H)]
        for c in range(n):
            piv = max(range(c, n), key=lambda r: abs(A[r][c]))
            A[c], A[piv] = A[piv], A[c]
            for r in range(n):
                if r != c:
                    k = A[r][c] / A[c][c]
                    for j in range(c, n + 1):
                        A[r][j] -= k * A[c][j]
        w = [a - A[i][n] / A[i][i] for i, a in enumerate(w)]
    return w, f


def held_out(reviews, cols, folds=5):
    ks = sorted({r[0] for r in reviews})
    random.Random(1).shuffle(ks)
    tot = 0.0
    for i in range(folds):
        te_k = set(ks[i::folds])
        tr = [r for r in reviews if r[0] not in te_k]
        te = [r for r in reviews if r[0] in te_k]
        w, f = fit_logistic(tr, cols)
        for r in te:
            p = min(max(1 / (1 + math.exp(-sum(a * b for a, b in zip(w, f(r))))), 1e-6), 1 - 1e-6)
            tot -= math.log(p if r[4] else 1 - p)
    return tot / len(reviews)


def main():
    vault, sessions = sys.argv[1], sys.argv[2]
    reviews, nk = history(vault, sessions)
    print(f"{len(reviews)} reviews of {nk} kanji; recall overall {100 * sum(r[4] for r in reviews) // len(reviews)}%")
    stages, mult, ready = {}, {}, True
    for name, (lo, hi) in STEPS.items():
        step = [r for r in reviews if (r[2] == int(name) if name != "4" else r[2] >= 4) and lo <= r[1] <= hi]
        n, ok = len(step), sum(r[4] for r in step)
        p = (ok + 0.5) / (n + 1) if n else None
        m = 1.0 if not n else math.exp(KAPPA * (n / (n + SHRINK)) * (logit(p) - logit(TARGET)))
        mult[name] = round(min(HI, max(LO, m)), 3)
        stages[name] = {"reviews": n, "recall": None if p is None else round(p, 3)}
        ready = ready and n >= NEED
        print(f"  step after success {name}{'+' if name == '4' else ''}: {n:4} reviews at the right gap, recall "
              f"{'-' if p is None else f'{100 * p:.0f}%'} -> multiplier {mult[name]}")
    base = held_out(reviews, ["b"])
    rows = {c: held_out(reviews, cols) for c, cols in (("gap", ["b", "lnt"]), ("streak", ["b", "lnt", "reps"]), ("streak+lapses", ["b", "lnt", "reps", "lapses"]))}
    print(f"held-out log-loss: constant {base:.4f}, " + ", ".join(f"+{c} {v:.4f}" for c, v in rows.items()))
    w, _ = fit_logistic(reviews, ["b", "lnt", "reps", "lapses"])
    longs = sum(1 for r in reviews if r[1] >= 15)
    print(f"gap coefficient {w[1]:+.3f} (0 = no forgetting measured), streak {w[2]:+.3f}, lapses {w[3]:+.3f}; reviews 15+ days apart: {longs}")
    path = os.path.join(q.VAULT, ".scheduler.json")
    old = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
    out = {"fitted": datetime.date.today().isoformat(), "reviews": len(reviews), "kanji": nk, "target": TARGET,
           "stages": stages, "multipliers": mult, "coef": dict(zip(["b", "ln_gap", "streak", "lapses"], [round(x, 3) for x in w])),
           "held_out_loss": {"constant": round(base, 4), **{c: round(v, 4) for c, v in rows.items()}},
           "reviews_15d_plus": longs, "ready": ready, "enabled": bool(old.get("enabled", False))}
    json.dump(out, open(path, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print(f"ready: {ready} (every step needs {NEED}+ reviews) | enabled: {out['enabled']} | written to {path}")


if __name__ == "__main__":
    main()
