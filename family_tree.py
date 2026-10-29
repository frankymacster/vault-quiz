#!/usr/bin/env python3
"""Write the Kanji Family Tree page for a vault: the same families the quiz's
--clusters mode uses (quiz.build_families), nested by each family head's own
parts from the IDS data, with your known / learning / hard / not-started
status on every character.

    python3 family_tree.py <vault> <out.html> [template]

template defaults to family_tree_template.html; family_game_template.html
writes the Kanji Family Builder game from the same data.
"""
import collections, datetime, json, os, re, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import quiz as q
import sound_families
import word_align
import build_kanji_vault as bk

q.use_vault(sys.argv[1])
kanji, vocab = q.build()
cm = q.load_component_meanings()
state = q.load_state()
fams, home = q.build_families(kanji)
pool = [k for k in home]

ids = {}
for line in open(os.path.join(os.path.dirname(HERE), ".jkgv_cache", "ids.txt"), encoding="utf-8"):
    if line.startswith("#") or "\t" not in line:
        continue
    f = line.rstrip("\n").split("\t")
    seqs = f[2:]
    pick = next((s for s in seqs if re.search(r"\[[A-Z]*J[A-Z]*\]$", s)), seqs[0] if seqs else f[1])
    ids[f[1]] = re.sub(r"\[[A-Z]+\]$", "", pick)
OPS = {chr(c) for c in range(0x2FF0, 0x2FFC)}


def sub_parts(c):
    return list(dict.fromkeys(x for x in ids.get(c, c)
                              if x not in OPS and not ("①" <= x <= "⑳") and x != c))


parent = {}
for c in fams:
    cand = [p for p in sub_parts(c) if p in fams and p != c]
    parent[c] = min(cand, key=lambda p: (len(fams[p]), p)) if cand else None
for c in list(parent):
    seen, x = {c}, parent[c]
    while x is not None:
        if x in seen:
            parent[c] = None
            break
        seen.add(x)
        x = parent[x]
children = collections.defaultdict(list)
for c, p in parent.items():
    if p:
        children[p].append(c)


def status(k):
    if kanji[k]["known"]:
        return "known"
    c = state.get(k)
    return "new" if not c else "hard" if q.is_hard(c) else "learning"


def head_meaning(c):
    if c in kanji:
        return q.short(kanji[c]["text"])
    m = cm.get(c, "")
    return "" if m.startswith("u+") else m.split(",")[0].strip()


levels = q.P["family_levels"]
rank = lambda k: (levels.index(q.level(kanji[k]["text"])), k)
data = {
    "generated": datetime.date.today().isoformat(),
    "levels": levels,
    "families": {c: {"m": head_meaning(c), "k": sorted(m, key=rank), "c": children.get(c, [])}
                 for c, m in fams.items()},
    "roots": [c for c in fams if parent[c] is None],
    "loners": sorted((k for k, c in home.items() if c is None), key=rank),
    "kanji": {k: [q.short(kanji[k]["text"]), q.level(kanji[k]["text"]), status(k), kanji[k]["comps"]]
              for k in pool},
}
kdb_path = os.path.join(os.path.dirname(HERE), ".jkgv_cache", "kanji.json")
if os.path.exists(kdb_path):
    kdb = json.load(open(kdb_path, encoding="utf-8"))
    sounds, roles, on = sound_families.build(kanji, pool, kdb)
    for k in pool:
        data["kanji"][k].append(on(k))
    data["sound"] = sounds
    data["roles"] = roles
# the quiz's schedule, so the game can aim at what's due: [due date, misses] per kanji in progress
card = lambda c: [datetime.date.fromordinal(c["due"]).isoformat(), c.get("lapses", 0), c.get("reps", 0),
                  c.get("ivl", 0), c.get("ease", 2.5)]
data["srs"] = {k: card(state[k]) for k in pool if k in state and not kanji[k]["known"] and state[k].get("due")}
# answers the game made before this moment are already in the schedule above
data["synced"] = int(time.time())
# the quiz's end-of-session progress table: [level, total, known, learning, close, hard]
prog = collections.defaultdict(lambda: [0, 0, 0, 0, 0])
for k, d in kanji.items():
    r = prog[q.level(d["text"])]
    r[0] += 1
    if d["known"]:
        r[1] += 1
    elif k in state:
        r[2] += 1
        r[3] += state[k].get("ivl", 0) >= 14
        r[4] += q.is_hard(state[k])
data["progress"] = [[lvl] + prog[lvl] for lvl in q.P["levels"] if prog[lvl][0]]
# words, for learning readings in context: [shown, reading, gloss, level, known, split, other readings]
if os.path.exists(kdb_path):
    vstate = q.load_state(os.path.join(q.VAULT, q.VOCAB_STATE_FILE))
    words, kun = {}, {}
    for w, d in vocab.items():
        if not q.vocab_ok(w, d):
            continue
        rs = [r for r in re.split(r"[\s/／、,・]+", q.shown_reading(w, d)) if r]
        if not rs:
            continue
        shown = re.split("[／/]", w)[0]
        full = q.meaning(d["text"])
        gloss = "; ".join(full.split(";")[:2]).strip()
        seg = word_align.align(shown, rs[0], kdb)
        words[w] = [shown, word_align.hira(rs[0]), gloss, q.level(d["text"]), int(d["known"]), seg,
                    sorted(q.accepted_readings(w, d)), full if full != gloss else 0]
        for c in shown:
            if c in kanji and c not in kun:
                kun[c] = word_align.readings_of(kdb.get(c, {}))[1][:6]
    data["words"], data["kun"] = words, kun
    data["stop"] = sorted(q.STOPWORDS)
    data["anchor"] = {k: kanji[k]["anchor"] for k in pool if kanji[k].get("anchor") in words}
    data["vsrs"] = {w: card(c) for w, c in vstate.items() if w in words and not words[w][4] and c.get("due")}
    vprog = collections.defaultdict(lambda: [0, 0, 0, 0, 0])
    for w, x in words.items():
        r = vprog[x[3]]
        r[0] += 1
        if x[4]:
            r[1] += 1
        elif w in vstate:
            r[2] += 1
            r[3] += vstate[w].get("ivl", 0) >= 14
            r[4] += q.is_hard(vstate[w])
    data["vprogress"] = [[lvl] + vprog[lvl] for lvl in q.P["levels"] if vprog[lvl][0]]
# wrong picks per kanji ({kanji: {pick: n}}, kept by game_sync): pairs picked twice get a look-first step
cpath = os.path.join(q.VAULT, ".game_confusions.json")
conf = json.load(open(cpath, encoding="utf-8")) if os.path.exists(cpath) else {}
data["confused"] = {k: {o: n for o, n in m.items() if o in pool} for k, m in conf.items() if k in pool}
# lookalikes (from the notes; game_sync adds your mix-ups), the meanings a typed answer is graded on, and mnemonics
data["looks"] = {k: [x for x in kanji[k]["lookalikes"] if x in pool] for k in pool if kanji[k]["lookalikes"]}
data["answer"] = {k: q.answer_text(kanji[k]["text"]) for k in pool}
data["mnem"] = {k: kanji[k]["mnemonic"] or kanji[k]["suggested"] for k in pool if kanji[k]["mnemonic"] or kanji[k]["suggested"]}
used = {c for k in pool for c in kanji[k]["comps"]}
data["parts"] = {c: (q.short(kanji[c]["text"]) if c in kanji else
                     ("" if cm.get(c, "").startswith("u+") else cm.get(c, "").split(",")[0].strip()))
                 for c in used}
if "sound" in data:
    data["partOn"] = {c: on(c)[0] for c in used if on(c)}
    # the meaning parts to learn first: most often the 'meaning' half of a kanji that has a sound part
    mcount = collections.Counter(c for k, r in roles.items() if "s" in r.values()
                                 for c, role in r.items() if role == "m")
    data["meaningRadicals"] = [[c, data["parts"].get(c, "")] for c, _ in mcount.most_common(24)]
# parts the IDS dictionary has no character for (it writes them (1)..(20)) were dropped from the notes: add each as a
# cut-out of its kanji, placed by the layout the dictionary gives (left half, top, inside a frame...)
ids_path = os.path.join(os.path.dirname(HERE), ".jkgv_cache", "ids.txt")
data["frag"] = {}
if os.path.exists(ids_path):
    ids = bk.load_ids(ids_path)
    OPS3 = set("⿲⿳")

    def parse(seq, i=0):
        c = seq[i]
        if c in bk.IDC:
            kids, j = [], i + 1
            for _ in range(3 if c in OPS3 else 2):
                node, j = parse(seq, j)
                kids.append(node)
            return (c, kids), j
        if c == "&":
            j = seq.index(";", i) + 1
            return (seq[i:j], None), j
        return (c, None), i + 1

    def children(op, bb, poly):
        x0, y0, x1, y1 = bb
        w, h = x1 - x0, y1 - y0
        P = lambda a, b: (x0 + a * w, y0 + b * h)
        box = lambda a, b, c, d: (x0 + a * w, y0 + b * h, x0 + c * w, y0 + d * h)
        ring = lambda bx: [(bx[0], bx[1]), (bx[2], bx[1]), (bx[2], bx[3]), (bx[0], bx[3])]
        if op in ("⿰", "⿲"):
            n = 3 if op == "⿲" else 2
            return [(b, ring(b)) for b in (box(i / n, 0, (i + 1) / n, 1) for i in range(n))]
        if op in ("⿱", "⿳"):
            n = 3 if op == "⿳" else 2
            return [(b, ring(b)) for b in (box(0, i / n, 1, (i + 1) / n) for i in range(n))]
        if op == "⿻":
            return [(bb, poly), (bb, poly)]
        inner, outer = {
            "⿴": (box(.2, .2, .8, .8), poly),
            "⿵": (box(.2, .3, .8, 1), [P(0, 0), P(1, 0), P(1, 1), P(.8, 1), P(.8, .3), P(.2, .3), P(.2, 1), P(0, 1)]),
            "⿶": (box(.2, 0, .8, .7), [P(0, 1), P(0, 0), P(.2, 0), P(.2, .7), P(.8, .7), P(.8, 0), P(1, 0), P(1, 1)]),
            "⿷": (box(.3, .2, 1, .8), [P(0, 0), P(1, 0), P(1, .2), P(.3, .2), P(.3, .8), P(1, .8), P(1, 1), P(0, 1)]),
            "⿸": (box(.35, .35, 1, 1), [P(0, 0), P(1, 0), P(1, .35), P(.35, .35), P(.35, 1), P(0, 1)]),
            "⿹": (box(0, .35, .65, 1), [P(0, 0), P(1, 0), P(1, 1), P(.65, 1), P(.65, .35), P(0, .35)]),
            "⿺": (box(.35, 0, 1, .65), [P(0, 0), P(.35, 0), P(.35, .65), P(1, .65), P(1, 1), P(0, 1)]),
        }[op]
        return [(bb, outer), (inner, ring(inner))]

    def walk(node, bb, poly, frame, out):
        op, kids = node
        if kids is None:
            out.append((op, poly, bb, frame))
            return
        for kid, (kbb, kpoly) in zip(kids, children(op, bb, poly)):
            walk(kid, kbb, kpoly, frame or (op in "⿴⿵⿶⿷⿸⿹⿺" and kid is kids[0]), out)

    def where(bb, frame):
        if frame:
            return "outer part"
        cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
        h = "left" if cx < .4 else "right" if cx > .6 else ""
        v = "top" if cy < .4 else "bottom" if cy > .6 else ""
        return "-".join(x for x in (v, h) if x) or "middle"

    css = lambda poly: "polygon(" + ", ".join("%.0f%% %.0f%%" % (x * 100, y * 100) for x, y in poly) + ")"
    rare = {}   # rare characters (CJK extensions B and later) many fonts cannot draw: the best cut-out to show instead
    for k in pool:
        seq = ids.get(k)
        if not seq:
            continue
        node, _ = parse(re.sub(r"&[^;]+;", "", seq))
        leaves = []
        walk(node, (0, 0, 1, 1), [(0, 0), (1, 0), (1, 1), (0, 1)], False, leaves)
        found = [(poly, bb, frame) for ch, poly, bb, frame in leaves if ch in bk.UNREPRESENTABLE]
        for n, (poly, bb, frame) in enumerate(found):
            pid = "◌" + k + (str(n + 1) if len(found) > 1 else "")
            data["frag"][pid] = {"k": k, "clip": css(poly)}
            data["parts"][pid] = "shape with no character: %s" % where(bb, frame)
            data["kanji"][k][3] = data["kanji"][k][3] + [pid]
        for ch, poly, bb, frame in leaves:
            if ord(ch) >= 0x20000 and ch in used and ch != k:
                score = (0 if frame else 1, (bb[2] - bb[0]) * (bb[3] - bb[1]))
                if ch not in rare or score > rare[ch][0]:
                    rare[ch] = (score, k, poly)
    for ch, (_, k, poly) in rare.items():
        data["frag"][ch] = {"k": k, "clip": css(poly), "rare": 1}
        if not data["parts"].get(ch):
            data["parts"][ch] = "rare shape"
    print("%d cut-out parts: %d with no character, %d rare characters some fonts cannot draw"
          % (len(data["frag"]), len(data["frag"]) - len(rare), len(rare)))
blob = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
assert "</script" not in blob
tpl = sys.argv[3] if len(sys.argv) > 3 else "family_tree_template.html"
template = open(os.path.join(HERE, tpl), encoding="utf-8").read()
open(sys.argv[2], "w", encoding="utf-8").write(template.replace("__DATA__", blob))
st = collections.Counter(v[2] for v in data["kanji"].values())
print(f"{len(fams)} families, {len(data['roots'])} top-level, {len(data['loners'])} on their own; {dict(st)}"
      + (f"; {len(data['sound'])} sound families" if "sound" in data else ""))
