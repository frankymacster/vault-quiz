#!/usr/bin/env python3
"""
Build an interlinked Obsidian vault for JLPT N2 study.

Graph shape:   Component  <-->  Kanji  <-->  Vocabulary
               (structure)     (node)       (phonetics + meaning)

Data sources (all fetched over plain HTTPS, cached locally):
  * kanji.json      davidluzgouveia/kanji-data      -> JLPT-new levels + meanings
  * ids.txt         cjkvi/cjkvi-ids (CHISE IDS)     -> visual decomposition (泉 = ⿱白水)
  * OpenJLPT vocab  evanclan/OpenJLPT               -> word / kana / gloss / level

Stdlib only.
"""

import json
import os
import re
import shutil
import sys
import unicodedata
import urllib.request
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VAULT = os.path.join(ROOT, "Japanese_Kanji_Graph_Vault")
CACHE = os.path.join(ROOT, ".jkgv_cache")

KANJI_URL = "https://raw.githubusercontent.com/davidluzgouveia/kanji-data/master/kanji.json"
IDS_URL = "https://raw.githubusercontent.com/cjkvi/cjkvi-ids/master/ids.txt"
VOCAB_URL = "https://raw.githubusercontent.com/evanclan/OpenJLPT/main/data/json/vocab/n{n}.json"

CORE_LEVELS = (5, 4, 3, 2)  # N5 -> N2

# ---------------------------------------------------------------- fetching


def fetch(url, filename):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, filename)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    print("  downloading %s" % url)
    req = urllib.request.Request(url, headers={"User-Agent": "kanji-vault-builder/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as out:
        shutil.copyfileobj(r, out)
    return path


# ---------------------------------------------------------------- decomposition

IDC = {chr(c) for c in range(0x2FF0, 0x2FFC)}          # ⿰⿱⿲... layout operators
UNREPRESENTABLE = {chr(c) for c in range(0x2460, 0x2474)}  # ①②③ = "component has no codepoint"


def load_ids(path):
    """char -> first-level component list, preferring the Japanese glyph variant."""
    table = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 3:
                continue
            char, seqs = parts[1], parts[2:]
            chosen = None
            for seq in seqs:
                tag = re.search(r"\[([A-Z]+)\]$", seq)
                if tag and "J" in tag.group(1):
                    chosen = seq
                    break
            if chosen is None:
                chosen = seqs[0]
            table[char] = re.sub(r"\[[A-Z]+\]$", "", chosen)
    return table


def decompose(char, ids_table):
    seq = ids_table.get(char, char)
    seq = re.sub(r"&[^;]+;", "", seq)  # CHISE private entities like &CDP-8BF5;
    parts = []
    for c in seq:
        if c in IDC or c in UNREPRESENTABLE or c == char:
            continue
        if unicodedata.category(c) in ("Cc", "Cf", "Zs"):
            continue
        if c not in parts:
            parts.append(c)
    return parts or [char]  # atomic kanji is its own component


# Readable names for the recurring bound forms that never stand alone as kanji.
COMPONENT_NAMES = {
    "氵": "water (3 drops)", "亻": "person (left)", "艹": "grass crown",
    "辶": "movement / road", "宀": "roof", "扌": "hand (left)",
    "忄": "heart (left)", "犭": "beast", "阝": "mound / village",
    "糸": "thread", "訁": "speech", "钅": "metal", "飠": "food",
    "衤": "clothing (left)", "礻": "altar / spirit", "⺌": "small crown",
    "⺈": "bound / wrap", "⺊": "divination", "⺆": "enclosure",
    "⺀": "ice (2 drops)", "夂": "walk slowly", "攵": "strike / action",
    "冫": "ice", "冖": "cover", "亠": "lid", "勹": "wrap", "匚": "box",
    "厶": "private", "廴": "stretch", "彳": "step / going",
    "彡": "hair / rays", "廾": "two hands", "尢": "lame", "巛": "river",
    "幺": "tiny thread", "廿": "twenty", "疒": "sickness", "癶": "footsteps",
    "禸": "track", "罒": "eye (net)", "耂": "old", "肀": "brush",
    "覀": "cover / west", "衣": "clothing", "邑": "village", "阜": "mound",
    "龶": "sprout top", "龷": "grass top", "㇒": "slash", "丿": "slash",
    "亅": "hook", "乚": "second / hook", "丶": "dot", "丨": "stick",
    "乀": "stroke", "乃": "from", "𠂉": "hair pin", "𠂇": "left hand",
    "𠂊": "bound hand", "𧾷": "foot (left)", "𦈢": "thread (left)",
    "俞": "generalization",
}


def component_meaning(char, kanji_db):
    if char in COMPONENT_NAMES:
        return COMPONENT_NAMES[char]
    entry = kanji_db.get(char)
    if entry and entry.get("meanings"):
        return ", ".join(m.lower() for m in entry["meanings"][:3])
    try:
        return unicodedata.name(char).replace("CJK UNIFIED IDEOGRAPH-", "U+").lower()
    except ValueError:
        return "—"


# ---------------------------------------------------------------- filenames

WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL", *("COM%d" % i for i in range(1, 10)),
    *("LPT%d" % i for i in range(1, 10)),
}
# illegal on Windows/macOS + characters Obsidian cannot put inside [[wikilinks]]
ILLEGAL = r'<>:"/\|?*[]#^{}'
TRANSLIT = {c: "＿" for c in ILLEGAL}
TRANSLIT.update({"/": "／", ":": "：", "?": "？", "*": "＊", "|": "｜", '"': "”"})


def sanitize(stem):
    out = []
    for ch in stem:
        if unicodedata.category(ch) in ("Cc", "Cf") or ch in ("　",):
            continue
        out.append(TRANSLIT.get(ch, ch) if ch in ILLEGAL else ch)
    stem = "".join(out).strip().rstrip(". ")
    if not stem:
        stem = "unnamed"
    if stem.split(".")[0].upper() in WIN_RESERVED:
        stem = "_" + stem
    return stem[:120]


class NameRegistry:
    """Guarantees one unique, filesystem-safe note name per entity, case-insensitively."""

    def __init__(self):
        self.by_key = {}
        self.taken = set()

    def assign(self, key, prefix, raw, hint=""):
        if key in self.by_key:
            return self.by_key[key]
        base = sanitize("%s-%s" % (prefix, raw))
        name = base
        if name.lower() in self.taken and hint:
            name = sanitize("%s (%s)" % (base, hint))
        n = 2
        while name.lower() in self.taken:
            name = "%s-%d" % (base, n)
            n += 1
        self.taken.add(name.lower())
        self.by_key[key] = name
        return name


# ---------------------------------------------------------------- build

def is_kanji(ch):
    return "一" <= ch <= "鿿" or "㐀" <= ch <= "䶿"


def is_japanese_text(word):
    """Reject the handful of upstream rows with mojibake (e.g. a Greek Ͼ inside a word)."""
    return all(is_kanji(c) or "ぁ" <= c <= "ゟ" or "ァ" <= c <= "ヿ" or c in "々〆ヶー・/（） "
               for c in word)


def level_tag(level):
    return "jlpt/n%d" % level if level else "jlpt/unlisted"


def refuse_if_curated():
    """The build regenerates the vault from scratch (rmtree, then write).
    Once a vault is its own git repo or holds quiz state, it carries work
    no build can recreate -- known tags, mnemonics, lookalikes, review
    history -- so stop before touching anything."""
    marks = [m for m in (".git", ".quiz_state.json") if os.path.exists(os.path.join(VAULT, m))]
    if marks:
        sys.exit(f"refusing to rebuild {VAULT}: it has {', '.join(marks)}, so it holds "
                 f"hand-made data this build would delete. Move it aside first if you "
                 f"really want a fresh vault.")


def main():
    refuse_if_curated()
    print("[1/6] fetching data sources")
    kanji_db = json.load(open(fetch(KANJI_URL, "kanji.json"), encoding="utf-8"))
    ids_table = load_ids(fetch(IDS_URL, "ids.txt"))

    raw_vocab = []
    for n in CORE_LEVELS:
        raw_vocab.extend(json.load(open(fetch(VOCAB_URL.format(n=n), "vocab-n%d.json" % n),
                                        encoding="utf-8")))
    print("      %d kanji entries, %d IDS entries, %d raw vocab rows"
          % (len(kanji_db), len(ids_table), len(raw_vocab)))

    print("[2/6] selecting kanji (N5-N2 core)")
    kanji_level = {}
    for ch, entry in kanji_db.items():
        lv = entry.get("jlpt_new")
        if lv in CORE_LEVELS:
            kanji_level[ch] = lv

    print("[3/6] normalising vocabulary")
    # merge duplicates on (word, reading); keep the easiest level it appears at
    merged = {}
    for row in raw_vocab:
        word = (row.get("word") or "").strip()
        if not word or not is_japanese_text(word) or not any(is_kanji(c) for c in word):
            continue  # kana-only words have no structural kanji -> no place in this graph
        reading = (row.get("reading") or "").strip() or word
        lv = int((row.get("level") or "N0")[1:])
        meanings = [m.strip() for m in (row.get("meanings") or []) if m and m.strip()]
        key = (word, reading)
        if key in merged:
            merged[key]["level"] = max(merged[key]["level"], lv)  # 5 = easiest
            for m in meanings:
                if m not in merged[key]["meanings"]:
                    merged[key]["meanings"].append(m)
        else:
            merged[key] = {"word": word, "reading": reading, "level": lv,
                           "meanings": meanings}

    # Any kanji used by an included word gets a node too, so no link ever dangles.
    for v in merged.values():
        for ch in v["word"]:
            if is_kanji(ch) and ch not in kanji_level:
                kanji_level[ch] = kanji_db.get(ch, {}).get("jlpt_new")

    vocab = sorted(merged.values(), key=lambda v: (-v["level"], len(v["word"]), v["word"]))
    extra = sum(1 for lv in kanji_level.values() if lv not in CORE_LEVELS)
    print("      %d vocabulary words, %d kanji (%d beyond the N5-N2 core)"
          % (len(vocab), len(kanji_level), extra))

    print("[4/6] building the graph")
    kanji_comps = {k: decompose(k, ids_table) for k in kanji_level}
    comp_meaning = {}
    comp_kanji = defaultdict(list)
    for k, comps in kanji_comps.items():
        for c in comps:
            comp_kanji[c].append(k)
            comp_meaning.setdefault(c, component_meaning(c, kanji_db))

    kanji_vocab = defaultdict(list)
    vocab_kanji = {}
    for v in vocab:
        seen = [c for i, c in enumerate(v["word"])
                if is_kanji(c) and c in kanji_level and c not in v["word"][:i]]
        vocab_kanji[(v["word"], v["reading"])] = seen
        for c in seen:
            kanji_vocab[c].append(v)

    names = NameRegistry()
    for c in sorted(comp_kanji):
        names.assign(("C", c), "Comp", c)
    for k in sorted(kanji_level):
        names.assign(("K", k), "Kanji", k)
    for v in vocab:
        names.assign(("V", v["word"], v["reading"]), "Vocab", v["word"], hint=v["reading"])

    print("      %d components, %d kanji, %d vocab notes"
          % (len(comp_kanji), len(kanji_level), len(vocab)))

    print("[5/6] writing notes")
    if os.path.isdir(VAULT):
        shutil.rmtree(VAULT)
    dirs = {d: os.path.join(VAULT, d) for d in ("Components", "Kanji", "Vocabulary")}
    for d in dirs.values():
        os.makedirs(d)

    def write(folder, name, body):
        with open(os.path.join(dirs[folder], name + ".md"), "w", encoding="utf-8") as fh:
            fh.write(body)

    def kanji_meaning(ch):
        e = kanji_db.get(ch, {})
        return ", ".join(m.lower() for m in e.get("meanings", [])[:3]) or "—"

    def kanji_also_means(ch):
        # senses past the first three: not shown as the gloss, but the quiz
        # grades against them (直 'fix', 無 'not')
        rest = kanji_db.get(ch, {}).get("meanings", [])[3:]
        return ", ".join(m.lower() for m in rest if m.strip("- "))

    def kanji_sort(ch):
        return (-(kanji_level.get(ch) or 0), kanji_db.get(ch, {}).get("freq") or 99999, ch)

    written = set()

    # --- Components
    for c in sorted(comp_kanji):
        name = names.by_key[("C", c)]
        lines = [
            "---",
            "tags: [japanese/component]",
            "---",
            "# Component: %s" % c,
            "**Meaning:** %s" % comp_meaning[c],
            "",
            "### 🔗 Downstream Kanji",
        ]
        for k in sorted(set(comp_kanji[c]), key=kanji_sort):
            lines.append("- [[%s]]" % names.by_key[("K", k)])
        lines += ["", "---", ""]
        write("Components", name, "\n".join(lines))
        written.add(name)

    # --- Kanji
    for k in sorted(kanji_level, key=kanji_sort):
        name = names.by_key[("K", k)]
        lv = kanji_level[k]
        lines = [
            "---",
            "tags: [japanese/kanji, %s]" % level_tag(lv),
            "---",
            "# Kanji: %s" % k,
            "**Meaning:** %s" % kanji_meaning(k),
        ] + (["**Also means:** %s" % kanji_also_means(k)] if kanji_also_means(k) else []) + [
            "",
            "### 🧱 Components",
        ]
        for c in kanji_comps[k]:
            lines.append("- [[%s]]" % names.by_key[("C", c)])
        lines += ["", "### 🗣️ Vocabulary"]
        vs = sorted(kanji_vocab.get(k, []),
                    key=lambda v: (-v["level"], len(v["word"]), v["word"]))
        if vs:
            for v in vs:
                lines.append("- [[%s]]" % names.by_key[("V", v["word"], v["reading"])])
        else:
            lines.append("- *(no N5–N2 vocabulary in this dataset)*")
        lines += ["", "---", ""]
        write("Kanji", name, "\n".join(lines))
        written.add(name)

    # --- Vocabulary
    for v in vocab:
        key = ("V", v["word"], v["reading"])
        name = names.by_key[key]
        lines = [
            "---",
            "tags: [japanese/vocabulary, %s]" % level_tag(v["level"]),
            "---",
            "# Vocabulary: %s" % v["word"],
            "**Reading (Kana):** %s" % v["reading"],
            "**Meaning:** %s" % ("; ".join(v["meanings"]) or "—"),
            "",
            "### ⚙️ Structural Kanji",
        ]
        for c in vocab_kanji[(v["word"], v["reading"])]:
            lines.append("- [[%s]]" % names.by_key[("K", c)])
        lines += ["", "---", ""]
        write("Vocabulary", name, "\n".join(lines))
        written.add(name)

    counts = {lv: sum(1 for x in kanji_level.values() if x == lv) for lv in CORE_LEVELS}
    with open(os.path.join(VAULT, "_Vault Info.md"), "w", encoding="utf-8") as fh:
        fh.write(
            "---\ntags: [japanese/index]\n---\n"
            "# Japanese Kanji Graph Vault\n\n"
            "Components → Kanji → Vocabulary. Open **Graph View** and colour by the\n"
            "`japanese/component`, `japanese/kanji` and `japanese/vocabulary` tags.\n\n"
            "| Layer | Notes |\n|---|---|\n"
            "| Components | %d |\n| Kanji | %d |\n| Vocabulary | %d |\n\n"
            "Kanji by level: N5 %d · N4 %d · N3 %d · N2 %d "
            "(+%d supporting kanji pulled in by vocabulary).\n\n"
            "## Attribution\n"
            "- JLPT levels & glosses: Jonathan Waller / tanos.co.uk (CC BY) via "
            "[OpenJLPT](https://github.com/evanclan/OpenJLPT) (CC BY-SA 4.0)\n"
            "- Readings & definitions: JMdict/EDICT, KANJIDIC2 — © EDRDG (CC BY-SA 4.0)\n"
            "- Decomposition: [CHISE IDS database](https://github.com/cjkvi/cjkvi-ids)\n"
            "- Kanji metadata: [davidluzgouveia/kanji-data](https://github.com/davidluzgouveia/kanji-data)\n"
            % (len(comp_kanji), len(kanji_level), len(vocab),
               counts[5], counts[4], counts[3], counts[2], extra)
        )

    print("[6/6] verifying graph integrity")
    errors = []
    on_disk = set()
    for folder in dirs.values():
        for f in os.listdir(folder):
            if f.endswith(".md"):
                on_disk.add(f[:-3])

    link_re = re.compile(r"\[\[([^\]]+)\]\]")
    edges = set()
    for folder, path in dirs.items():
        for f in sorted(os.listdir(path)):
            if not f.endswith(".md"):
                continue
            src = f[:-3]
            text = open(os.path.join(path, f), encoding="utf-8").read()
            for tgt in link_re.findall(text):
                if tgt not in on_disk:
                    errors.append("dangling link: %s -> %s" % (src, tgt))
                edges.add((src, tgt))

    for a, b in edges:
        if (b, a) not in edges:
            errors.append("missing backlink: %s -> %s but not %s -> %s" % (a, b, b, a))

    if errors:
        print("      ✗ %d problem(s):" % len(errors))
        for e in errors[:20]:
            print("        " + e)
        sys.exit(1)

    print("      ✓ %d notes, %d links, every link resolves and is bidirectional"
          % (len(on_disk), len(edges)))
    print("\nVault written to %s" % VAULT)


if __name__ == "__main__":
    main()
