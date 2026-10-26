#!/usr/bin/env python3
"""Fold Kanji Family Builder answers into the quiz's review schedule.

The game runs in the browser and saves answers to its own database; this
reads those session documents (as exported to JSON files) and applies them
to the vault's .quiz_state.json with the quiz's own rules:

  - a wrong meaning on a kanji you're learning is a lapse, exactly as a
    miss in the quiz: it comes back in your next session;
  - a right meaning counts as a review only when the kanji was due that
    day -- picking from four is weaker evidence than typed recall, and
    answering before it's due doesn't lengthen how long it lasts;
  - kanji you haven't started aren't added (the game shows their meaning
    first, so a right answer isn't recall), except in New kanji mode,
    where a kanji is studied and then tested: that test starts it, as in
    the quiz; kanji tagged known that you miss are reported, not un-tagged.

A right answer the player marked "not sure" counts as a hard review (grade 2:
a shorter next interval) rather than a good one. Every wrong pick is also
tallied in .game_confusions.json ({kanji: {pick: n}}), which the game uses to
look first at pairs that have been mixed up twice.

Words mode answers go to the vocabulary schedule (.vocab_state.json, the
quiz's --vocab mode): a word is right when both its reading and its meaning
are (in Build, a kanji's anchor word is asked for its reading only). A new word is studied in the game before it's tested, so its first test
starts it, as in the quiz; after that the same rules as kanji apply.

Each event is applied once: imported counts per session document are kept
in .game_import.json, so re-running is safe.

    python3 game_sync.py <vault> <sessions-dir>
"""
import datetime, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import quiz as q

vault, src = sys.argv[1], sys.argv[2]
q.use_vault(vault)
kanji, vocab = q.build()
state = q.load_state()
cpath = os.path.join(q.VAULT, ".game_confusions.json")
confusions = json.load(open(cpath, encoding="utf-8")) if os.path.exists(cpath) else {}
vpath = os.path.join(q.VAULT, q.VOCAB_STATE_FILE)
vstate = q.load_state(vpath)
done_path = os.path.join(q.VAULT, ".game_import.json")
done = json.load(open(done_path, encoding="utf-8")) if os.path.exists(done_path) else {}

new = []
for fn in sorted(os.listdir(src)):
    if not fn.endswith(".json"):
        continue
    sid = fn[:-5]
    doc = json.load(open(os.path.join(src, fn), encoding="utf-8"))
    events = doc.get("data", doc).get("events", [])
    start = done.get(sid, 0)
    new += [(e, sid) for e in events[start:]]
    done[sid] = len(events)
new.sort(key=lambda p: p[0].get("t", 0))

real_today = q.today
lapses, reviews, early, unstarted, known_missed, matured = [], [], 0, 0, [], []
new_looks, started = [], []
w_started, w_lapses, w_reviews, w_early, w_matured, w_known_missed = [], [], [], 0, [], []
for e, sid in new:
    if e.get("step") == "word" and e.get("w") in vocab:
        w, ok = e["w"], e["ok"]
        day = datetime.date.fromtimestamp(e["t"]).toordinal()
        q.today = lambda d=day: d
        if vocab[w]["known"]:
            if not ok:
                w_known_missed.append(w)
            continue
        card = vstate.get(w)
        if card is None:
            vstate[w] = q.schedule({"intro": day}, 3 if ok else 1)
            w_started.append(w)
        elif not ok:
            q.schedule(card, 1)
            w_lapses.append(w)
        elif card.get("due", 0) <= day:
            q.schedule(card, 3)
            w_reviews.append(w)
            if card["ivl"] >= q.MATURE:
                q.mark_known(vocab[w])
                w_matured.append(w)
        else:
            w_early += 1
        continue
    if e.get("step") != "meaning" or e.get("k") not in kanji:
        continue
    k, ok = e["k"], e["ok"]
    if not ok and e.get("picked") in kanji and e["picked"] != k:
        confusions.setdefault(k, {})
        confusions[k][e["picked"]] = confusions[k].get(e["picked"], 0) + 1
    # a pick that shares a part with the kanji (or is one: 然/燃) is a confusion observed:
    # both notes list it under Lookalikes
    other = e.get("picked")
    if not ok and other in kanji and other != k and (set(kanji[k]["comps"]) & set(kanji[other]["comps"])
                                                      or other in kanji[k]["comps"] or k in kanji[other]["comps"]):
        if other not in kanji[k]["lookalikes"] or k not in kanji[other]["lookalikes"]:
            q.add_lookalike(kanji[k], other)
            q.add_lookalike(kanji[other], k)
            new_looks.append(k + other)
    if kanji[k]["known"]:
        if not ok:
            known_missed.append(k)
        continue
    card = state.get(k)
    day = datetime.date.fromtimestamp(e["t"]).toordinal()
    q.today = lambda d=day: d                  # schedule from the day it was answered
    if card is None:
        # New kanji mode: studied first, so its first test starts it, as in the quiz
        if e.get("fresh"):
            state[k] = q.schedule({"intro": day}, 3 if ok else 1)
            started.append(k)
        else:
            unstarted += 1
        continue
    if not ok:
        q.schedule(card, 1)
        lapses.append(k)
    elif card.get("due", 0) <= day:
        q.schedule(card, 2 if e.get("conf") == "unsure" else 3)
        reviews.append(k)
        if card["ivl"] >= q.MATURE:
            q.mark_known(kanji[k])
            matured.append(k)
    else:
        early += 1
q.today = real_today

q.save_state(state)
json.dump(confusions, open(cpath, "w", encoding="utf-8"), ensure_ascii=False, indent=1, sort_keys=True)
if w_started or w_lapses or w_reviews:
    q.save_state(vstate, vpath)
json.dump(done, open(done_path, "w", encoding="utf-8"), indent=1, sort_keys=True)
print(f"{len(new)} new game events; {len(lapses)} misses -> back in your next session"
      f"{' (' + ''.join(lapses) + ')' if lapses else ''}; {len(reviews)} due kanji reviewed"
      f"{' (' + ''.join(reviews) + ')' if reviews else ''}; {early} right but not due (no change); "
      f"{unstarted} on kanji not started; {len(matured)} matured to known"
      + (f"; known kanji missed in the game: {''.join(dict.fromkeys(known_missed))}" if known_missed else ""))
if any((w_started, w_lapses, w_reviews, w_early, w_known_missed)):
    print(f"words: {len(w_started)} started, {len(w_lapses)} misses -> due again"
          f"{' (' + ' '.join(dict.fromkeys(w_lapses)) + ')' if w_lapses else ''}; {len(w_reviews)} due words reviewed; "
          f"{w_early} right but not due; {len(w_matured)} matured to known"
          + (f"; known words missed: {' '.join(dict.fromkeys(w_known_missed))}" if w_known_missed else ""))
if new_looks:
    print(f"lookalikes added from your mix-ups: {' '.join(dict.fromkeys(new_looks))}")
if started:
    print(f"new kanji started: {''.join(started)}")
