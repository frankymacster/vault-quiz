#!/usr/bin/env python3
"""Write the Kanji Family Tree page for a vault: the same families the quiz's
--clusters mode uses (quiz.build_families), nested by each family head's own
parts from the IDS data, with your known / learning / hard / not-started
status on every character.

    python3 family_tree.py <vault> <out.html> [template]

template defaults to family_tree_template.html; family_game_template.html
writes the Kanji Family Builder game from the same data.
"""
import collections, datetime, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import quiz as q
import sound_families

q.use_vault(sys.argv[1])
kanji, _ = q.build()
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
used = {c for k in pool for c in kanji[k]["comps"]}
data["parts"] = {c: (q.short(kanji[c]["text"]) if c in kanji else
                     ("" if cm.get(c, "").startswith("u+") else cm.get(c, "").split(",")[0].strip()))
                 for c in used}
if "sound" in data:
    data["partOn"] = {c: on(c)[0] for c in used if on(c)}
blob = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
assert "</script" not in blob
tpl = sys.argv[3] if len(sys.argv) > 3 else "family_tree_template.html"
template = open(os.path.join(HERE, tpl), encoding="utf-8").read()
open(sys.argv[2], "w", encoding="utf-8").write(template.replace("__DATA__", blob))
st = collections.Counter(v[2] for v in data["kanji"].values())
print(f"{len(fams)} families, {len(data['roots'])} top-level, {len(data['loners'])} on their own; {dict(st)}"
      + (f"; {len(data['sound'])} sound families" if "sound" in data else ""))
