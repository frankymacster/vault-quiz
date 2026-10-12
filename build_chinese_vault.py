#!/usr/bin/env python3
"""
Build an interlinked Obsidian vault for Mandarin Chinese (HSK 3.0 levels 1-6).

Japanese:  Component -> Kanji     -> Vocabulary
Chinese:   Component -> Character -> Vocabulary
                     \-> Phonetic Series

Chinese is where this graph shape actually originates, but it has one lever
Japanese does not exploit: most hanzi are 形声字 — a semantic radical plus a
*phonetic* component. 请 qǐng is 讠(speech) + 青 qīng. Grouping characters by
their phonetic component turns unfamiliar hanzi into readable ones, so that
gets its own layer. (73% of makemeahanzi's 9,574 characters are pictophonetic;
the exact share within this vault is printed at build time.)

Data sources (all fetched over HTTPS, cached in .ckgv_cache/, ~12 MB total):
  * drkameleon/complete-hsk-vocabulary  -> HSK 3.0 words, pinyin, glosses, traditional
  * skishore/makemeahanzi               -> decomposition + pictophonetic etymology
  * elkmovie/hsk30 charlist             -> per-character HSK level
  * OpenCC STCharacters                 -> character-level simplified -> traditional

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
VAULT = os.path.join(ROOT, "Chinese_Hanzi_Graph_Vault")
CACHE = os.path.join(ROOT, ".ckgv_cache")

RAW = "https://raw.githubusercontent.com"
HSK_URL = RAW + "/drkameleon/complete-hsk-vocabulary/main/complete.json"
MMAH_URL = RAW + "/skishore/makemeahanzi/master/dictionary.txt"
CHARLIST_URL = RAW + "/elkmovie/hsk30/main/charlist.txt"
OPENCC_URL = RAW + "/BYVoid/OpenCC/master/data/dictionary/STCharacters.txt"
IDS_URL = RAW + "/cjkvi/cjkvi-ids/master/ids.txt"

LEVELS = [1, 2, 3, 4, 5, 6]          # HSK 3.0 "new-N"
IDC = {chr(c) for c in range(0x2FF0, 0x2FFC)}
UNKNOWN_PARTS = {"？", "?"}


def fetch(url, filename, note=""):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, filename)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    print("      downloading %s%s" % (filename, note))
    req = urllib.request.Request(url, headers={"User-Agent": "chinese-vault-builder/1.0"})
    with urllib.request.urlopen(req, timeout=300) as r, open(path + ".part", "wb") as out:
        shutil.copyfileobj(r, out, 1 << 20)
    os.replace(path + ".part", path)
    return path


def is_hanzi(ch):
    return "一" <= ch <= "鿿" or "㐀" <= ch <= "䶿" or "豈" <= ch <= "﫿"


# ---------------------------------------------------------------- component naming

COMPONENT_NAMES = {
    "讠": "speech radical (words, saying)", "氵": "water radical (3 drops)",
    "亻": "person radical", "扌": "hand radical", "忄": "heart radical",
    "艹": "grass radical", "辶": "movement / walking radical",
    "宀": "roof radical", "犭": "animal radical", "阝": "mound (left) / city (right)",
    "钅": "metal radical", "饣": "food radical", "纟": "silk radical",
    "衤": "clothing radical", "礻": "ritual / spirit radical", "疒": "sickness radical",
    "灬": "fire radical (4 dots)", "刂": "knife radical (right)",
    "冫": "ice radical", "冖": "cover radical", "亠": "lid radical",
    "勹": "wrap radical", "匚": "box radical", "厶": "private",
    "彳": "step / going radical", "彡": "hair / rays", "夂": "walk slowly",
    "攵": "strike / action radical", "廴": "stretch", "廾": "two hands",
    "巛": "river", "幺": "tiny thread", "廿": "twenty", "癶": "footsteps",
    "罒": "eye / net radical", "耂": "old radical", "覀": "cover / west",
    "丬": "split wood (left)", "爫": "claw radical", "⺈": "wrap / bound",
    "𠂇": "left hand", "𠂉": "hair pin", "丿": "slash", "亅": "hook",
    "乚": "hidden / hook", "丶": "dot", "丨": "vertical stroke", "一": "one",
    "⺊": "divination", "⺌": "small crown", "⺀": "ice (2 drops)",
}


def component_meaning(ch, char_db):
    if ch in COMPONENT_NAMES:
        return COMPONENT_NAMES[ch]
    entry = char_db.get(ch)
    if entry and entry.get("definition"):
        return entry["definition"]
    try:
        return unicodedata.name(ch).replace("CJK UNIFIED IDEOGRAPH-", "U+").lower()
    except ValueError:
        return "—"


# ---------------------------------------------------------------- filenames

WIN_RESERVED = {"CON", "PRN", "AUX", "NUL",
                *("COM%d" % i for i in range(1, 10)),
                *("LPT%d" % i for i in range(1, 10))}
ILLEGAL = r'<>:"/\|?*[]#^{}'
TRANSLIT = {"/": "／", ":": "：", "?": "？", "*": "＊", "|": "｜", '"': "”",
            "<": "＜", ">": "＞", "\\": "＼", "[": "（", "]": "）",
            "#": "＃", "^": "＾", "{": "｛", "}": "｝"}


def sanitize(stem):
    out = []
    for ch in stem:
        if unicodedata.category(ch) in ("Cc", "Cf"):
            continue
        out.append(TRANSLIT.get(ch, "_") if ch in ILLEGAL else ch)
    stem = unicodedata.normalize("NFC", "".join(out)).strip().rstrip(". ")
    if not stem:
        stem = "unnamed"
    if stem.split(".")[0].upper() in WIN_RESERVED:
        stem = "_" + stem
    return stem[:120]


class NameRegistry:
    def __init__(self):
        self.by_key = {}
        self.taken = set()

    @staticmethod
    def fold(name):
        return unicodedata.normalize("NFC", name).casefold()

    def assign(self, key, prefix, raw, hint=""):
        if key in self.by_key:
            return self.by_key[key]
        base = sanitize("%s-%s" % (prefix, raw))
        name = base
        if self.fold(name) in self.taken and hint:
            name = sanitize("%s (%s)" % (base, hint))
        n = 2
        while self.fold(name) in self.taken:
            name = "%s-%d" % (base, n)
            n += 1
        self.taken.add(self.fold(name))
        self.by_key[key] = name
        return name


# ---------------------------------------------------------------- main

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
    hsk = json.load(open(fetch(HSK_URL, "hsk-complete.json", " (9.8 MB)"), encoding="utf-8"))

    char_db = {}
    for line in open(fetch(MMAH_URL, "makemeahanzi.txt", " (2.5 MB)"), encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        char_db[rec["character"]] = rec

    char_level = {}
    cur = None
    for line in open(fetch(CHARLIST_URL, "hsk30-charlist.txt"), encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        if "\t" not in line and line.endswith("表"):
            # section header. The file ends with 初等手写字表 (elementary handwriting),
            # which is NOT a level band — without this it inherits level 7 and
            # mislabels ~1200 characters.
            m = re.match(r"^([一二三四五六七八九十]+)级汉字表$", line)
            cur = ({"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}
                   .get(m.group(1)[0], 7)) if m else None
            continue
        parts = line.split("\t")
        ch = parts[-1].strip()
        if len(ch) == 1 and is_hanzi(ch) and cur:
            char_level.setdefault(ch, cur)

    s2t = {}
    for line in open(fetch(OPENCC_URL, "STCharacters.txt"), encoding="utf-8"):
        if line.startswith("#") or "\t" not in line:
            continue
        k, v = line.rstrip("\n").split("\t", 1)
        s2t[k] = v.split(" ")[0]

    ids_table = {}
    for line in open(fetch(IDS_URL, "ids.txt", " (2.2 MB)"), encoding="utf-8"):
        if line.startswith("#"):
            continue
        p = line.rstrip("\n").split("\t")
        if len(p) < 3:
            continue
        ch, seqs = p[1], p[2:]
        chosen = None
        for seq in seqs:                       # prefer the mainland (G) glyph shape
            tag = re.search(r"\[([A-Z]+)\]$", seq)
            if tag and "G" in tag.group(1):
                chosen = seq
                break
        ids_table[ch] = re.sub(r"\[[A-Z]+\]$", "", chosen or seqs[0])

    print("      %d HSK records, %d characters in makemeahanzi, %d HSK-graded chars"
          % (len(hsk), len(char_db), len(char_level)))

    # ------------------------------------------------------------ vocabulary
    print("[2/6] selecting HSK 3.0 levels 1-6")
    wanted = {"new-%d" % n for n in LEVELS}
    vocab = {}
    for rec in hsk:
        levels = [l for l in rec.get("level", []) if l in wanted]
        if not levels:
            continue
        word = rec.get("simplified", "")
        if not word or not any(is_hanzi(c) for c in word):
            continue
        lvl = min(int(l.split("-")[1]) for l in levels)
        forms = rec.get("forms") or [{}]
        meanings, trad, pinyin = [], "", ""
        for f in forms:
            for m in f.get("meanings", []):
                if m not in meanings:
                    meanings.append(m)
            trad = trad or f.get("traditional", "")
            pinyin = pinyin or (f.get("transcriptions") or {}).get("pinyin", "")
        cur = vocab.get(word)
        if cur and cur["level"] <= lvl:
            continue
        vocab[word] = {"word": word, "level": lvl, "pinyin": pinyin,
                       "traditional": trad if trad and trad != word else "",
                       "meanings": meanings,
                       "freq": rec.get("frequency") or 999999,
                       "pos": rec.get("pos") or []}
    print("      %d words" % len(vocab))

    # ------------------------------------------------------------ characters
    print("[3/6] decomposing characters")
    chars = {}
    for w in vocab:
        for ch in w:
            if is_hanzi(ch):
                chars.setdefault(ch, {})

    def decompose(ch):
        rec = char_db.get(ch)
        seq = (rec or {}).get("decomposition") or ids_table.get(ch, ch)
        seq = re.sub(r"&[^;]+;", "", seq)
        parts = []
        for c in seq:
            if c in IDC or c in UNKNOWN_PARTS or c == ch:
                continue
            if unicodedata.category(c) in ("Cc", "Cf", "Zs"):
                continue
            if not (is_hanzi(c) or "⺀" <= c <= "⿟"):
                continue
            if c not in parts:
                parts.append(c)
        return parts or [ch]

    phonetic_of = {}
    for ch in chars:
        rec = char_db.get(ch, {})
        ety = rec.get("etymology") or {}
        chars[ch] = {
            "char": ch,
            "meaning": (rec.get("definition") or "").strip() or "—",
            "traditional": s2t.get(ch, "") if s2t.get(ch, ch) != ch else "",
            "level": char_level.get(ch),
            "components": decompose(ch),
            "radical": rec.get("radical", ""),
            "pinyin": (rec.get("pinyin") or [""])[0],
        }
        if ety.get("type") == "pictophonetic" and ety.get("phonetic"):
            phonetic_of[ch] = ety["phonetic"]

    comp_chars = defaultdict(list)
    for ch, meta in chars.items():
        for c in meta["components"]:
            comp_chars[c].append(ch)

    char_vocab = defaultdict(list)
    vocab_chars = {}
    for w, v in vocab.items():
        seen = [c for i, c in enumerate(w) if is_hanzi(c) and c not in w[:i]]
        vocab_chars[w] = seen
        for c in seen:
            char_vocab[c].append(w)

    print("      %d characters, %d distinct components, %d pictophonetic"
          % (len(chars), len(comp_chars), len(phonetic_of)))

    # ------------------------------------------------------------ phonetic series
    print("[4/6] building phonetic series")
    series = defaultdict(list)
    for ch, phon in phonetic_of.items():
        series[phon].append(ch)
    series = {p: sorted(cs) for p, cs in series.items() if len(cs) >= 2}
    print("      %d series with 2+ characters in this vault, covering %d characters"
          % (len(series), sum(len(v) for v in series.values())))

    # ------------------------------------------------------------ graph
    print("[5/6] writing notes")
    names = NameRegistry()
    for c in sorted(comp_chars):
        names.assign(("C", c), "Comp", c)
    for c in sorted(chars):
        names.assign(("H", c), "Hanzi", c)
    for w in sorted(vocab):
        names.assign(("V", w), "Vocab", w)
    for p in sorted(series):
        names.assign(("P", p), "Phon", p)

    if os.path.isdir(VAULT):
        shutil.rmtree(VAULT)
    dirs = {d: os.path.join(VAULT, d) for d in
            ("Components", "Characters", "Vocabulary", "Phonetic")}
    for d in dirs.values():
        os.makedirs(d)

    def write(folder, name, lines):
        path = os.path.join(dirs[folder], unicodedata.normalize("NFC", name) + ".md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines + ["", "---", ""]))

    def char_sort(ch):
        return (chars[ch]["level"] or 99, ch)

    def word_sort(w):
        return (vocab[w]["level"], vocab[w]["freq"], w)

    def level_tag(lv, kind="hsk"):
        return "%s/%s" % (kind, lv) if lv else "%s/unlisted" % kind

    # --- Components
    for c in sorted(comp_chars):
        lines = [
            "---",
            "tags: [chinese/component]",
            "---",
            "# Component: %s" % c,
            "**Meaning:** %s" % component_meaning(c, char_db),
            "",
            "### 🔗 Downstream Characters",
        ]
        lines += ["- [[%s]]" % names.by_key[("H", ch)]
                  for ch in sorted(set(comp_chars[c]), key=char_sort)]
        if c in series:
            lines += ["", "### 🔊 Phonetic Series",
                      "- [[%s]]" % names.by_key[("P", c)]]
        write("Components", names.by_key[("C", c)], lines)

    # --- Characters
    for ch in sorted(chars, key=char_sort):
        meta = chars[ch]
        lines = [
            "---",
            "tags: [chinese/character, %s]" % level_tag(meta["level"]),
            "---",
            "# Character: %s" % ch,
            "**Meaning:** %s" % meta["meaning"],
        ]
        if meta["traditional"]:
            lines.append("**Traditional:** %s" % meta["traditional"])
        lines += ["", "### 🧱 Components"]
        lines += ["- [[%s]]" % names.by_key[("C", c)] for c in meta["components"]]
        if ch in phonetic_of and phonetic_of[ch] in series:
            lines += ["", "### 🔊 Phonetic Series",
                      "- [[%s]]" % names.by_key[("P", phonetic_of[ch])]]
        lines += ["", "### 🗣️ Vocabulary"]
        vs = sorted(set(char_vocab.get(ch, [])), key=word_sort)
        lines += ["- [[%s]]" % names.by_key[("V", w)] for w in vs] or \
                 ["- *(no HSK 1–6 word in this vault uses this character)*"]
        write("Characters", names.by_key[("H", ch)], lines)

    # --- Vocabulary
    for w in sorted(vocab, key=word_sort):
        v = vocab[w]
        tags = ["chinese/vocabulary", "hsk/%d" % v["level"]]
        lines = [
            "---",
            "tags: [%s]" % ", ".join(tags),
            "---",
            "# Vocabulary: %s" % w,
            "**Pinyin:** %s" % (v["pinyin"] or "—"),
        ]
        if v["traditional"]:
            lines.append("**Traditional:** %s" % v["traditional"])
        lines.append("**Meaning:** %s" % ("; ".join(v["meanings"]) or "—"))
        lines += ["", "### ⚙️ Structural Characters"]
        lines += ["- [[%s]]" % names.by_key[("H", c)] for c in vocab_chars[w]]
        write("Vocabulary", names.by_key[("V", w)], lines)

    # --- Phonetic series
    for p in sorted(series):
        members = sorted(series[p], key=char_sort)
        phon_reading = (char_db.get(p, {}).get("pinyin") or ["—"])[0]
        lines = [
            "---",
            "tags: [chinese/phonetic-series]",
            "---",
            "# Phonetic Series: %s" % p,
            "**Component reading:** %s" % phon_reading,
            "**Members in this vault:** %d" % len(members),
            "",
            "> These characters all borrow %s for its *sound*, not its meaning."
            % p,
            "",
            "### 🎯 Characters",
        ]
        for ch in members:
            lines.append("- [[%s]] — %s — %s" % (
                names.by_key[("H", ch)], chars[ch]["pinyin"] or "?",
                chars[ch]["meaning"].split(",")[0].split(";")[0].strip()))
        if p in comp_chars:
            lines += ["", "### 🧱 As a Component", "- [[%s]]" % names.by_key[("C", p)]]
        write("Phonetic", names.by_key[("P", p)], lines)

    counts = {lv: sum(1 for v in vocab.values() if v["level"] == lv) for lv in LEVELS}
    biggest = sorted(series.items(), key=lambda kv: -len(kv[1]))[:8]
    with open(os.path.join(VAULT, "_Vault Info.md"), "w", encoding="utf-8") as fh:
        fh.write(
            "---\ntags: [chinese/index]\n---\n"
            "# Chinese Hanzi Graph Vault\n\n"
            "Components → Characters → Vocabulary, plus a **Phonetic Series** layer.\n\n"
            "| Layer | Notes |\n|---|---|\n"
            "| Components | %d |\n| Characters | %d |\n| Vocabulary | %d |\n"
            "| Phonetic series | %d |\n\n"
            "Words by HSK 3.0 level: %s.\n\n"
            "## Why the phonetic layer\n"
            "%.0f%% of the characters here are 形声字 — a semantic radical plus a\n"
            "component borrowed purely for its *sound*. 请 is 讠(speech) + 青 qīng.\n"
            "Learn 青 once and 请/清/情/晴/睛 stop being five unrelated shapes.\n\n"
            "Largest series in this vault: %s.\n\n"
            "## Attribution\n"
            "- HSK 3.0 word list, pinyin, glosses: "
            "[drkameleon/complete-hsk-vocabulary](https://github.com/drkameleon/complete-hsk-vocabulary) (MIT)\n"
            "- Decomposition & pictophonetic etymology: "
            "[makemeahanzi](https://github.com/skishore/makemeahanzi) (LGPL-3.0, from Unihan + CJKlib)\n"
            "- Per-character HSK levels: [elkmovie/hsk30](https://github.com/elkmovie/hsk30) "
            "(MIT, © Pleco Inc.)\n"
            "- Simplified→traditional: [OpenCC](https://github.com/BYVoid/OpenCC) (Apache-2.0)\n"
            "- Fallback decomposition: [CHISE IDS](https://github.com/cjkvi/cjkvi-ids)\n"
            % (len(comp_chars), len(chars), len(vocab), len(series),
               " · ".join("HSK%d %d" % (lv, counts[lv]) for lv in LEVELS),
               100.0 * len(phonetic_of) / max(len(chars), 1),
               ", ".join("%s (%d)" % (p, len(m)) for p, m in biggest))
        )

    # ------------------------------------------------------------ verify
    print("[6/6] verifying graph integrity")
    on_disk = set()
    for path in dirs.values():
        for f in os.listdir(path):
            if f.endswith(".md"):
                on_disk.add(unicodedata.normalize("NFC", f[:-3]))

    errors, edges = [], set()
    link_re = re.compile(r"\[\[([^\]]+)\]\]")
    for path in dirs.values():
        for f in sorted(os.listdir(path)):
            if not f.endswith(".md"):
                continue
            src = unicodedata.normalize("NFC", f[:-3])
            text = unicodedata.normalize(
                "NFC", open(os.path.join(path, f), encoding="utf-8").read())
            for tgt in link_re.findall(text):
                if tgt not in on_disk:
                    errors.append("dangling link: %s -> %s" % (src, tgt))
                edges.add((src, tgt))

    for a, b in edges:
        if (b, a) not in edges:
            errors.append("missing backlink: %s -> %s but not back" % (a, b))

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
