#!/usr/bin/env python3
"""
Build an interlinked Obsidian vault for German (A1-B2), mirroring the
Japanese Kanji graph vault but adapted to German word formation.

Japanese:  Component  ->  Kanji  ->  Vocabulary
German:    Morpheme   ->  Stem   ->  Vocabulary
           (ver-, -ung)  (fahren)   (Erfahrung, Abfahrt, Fahrzeug)

A kanji is a fixed node with fixed parts. A German stem is a *recombining*
node: `fahren` surfaces in erfahren / Erfahrung / abfahren / Fahrzeug, each
time wrapped in different affixes. So the middle layer is the base lexeme and
the affixes are the components.

Extra layer requested: /Ablaut for strong-verb vowel gradation series.

Data sources (all fetched over HTTPS, cached in .gkgv_cache/):
  * Goethe CEFR wordlists A1/A2/B1/B2   -> word selection, gloss, article, plural
  * kaikki.org Wiktionary (noun, verb)  -> gender, plural, principal parts, etymology
  * kaikki.org (prefix, suffix)         -> authored morpheme definitions
  * DErivBase v2.0                      -> derivational families
  * CharSplit (MIT, vendored)           -> compound splitting
  * hermitdave FrequencyWords de_50k    -> lexicon gate + ordering

Stdlib only; CharSplit is vendored into the cache directory, not pip-installed.
"""

import io
import json
import os
import re
import shutil
import sys
import unicodedata
import urllib.request
import zipfile
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VAULT = os.path.join(ROOT, "German_Morpheme_Graph_Vault")
CACHE = os.path.join(ROOT, ".gkgv_cache")

RAW = "https://raw.githubusercontent.com"
CEFR_SOURCES = {
    "A1": RAW + "/ilkermeliksitki/goethe-institute-wordlist/main/a1/a1.tsv",
    "A2": RAW + "/codersasank/ankideck-german-a2-wordlist/main/A2_Wortliste.csv",
    "B1": RAW + "/codersasank/ankideck-german-b1-wordlist/main/B1_Wortliste_Goethe.csv",
    "B2": RAW + "/magdalena-trivina/goethe-zertifikat-b2-wortliste/main/B2_Vocabs_All.csv",
}
KAIKKI = ("https://kaikki.org/dictionary/German/pos-%(p)s/"
          "kaikki.org-dictionary-German-by-pos-%(p)s.jsonl")
DERIVBASE = ("https://www.ims.uni-stuttgart.de/documents/ressourcen/lexika/"
             "derivbase/derivbase-v2.0.zip")
CHARSPLIT = RAW + "/dtuggener/CharSplit/master/charsplit/%s"
FREQ = RAW + "/hermitdave/FrequencyWords/master/content/2018/de/de_50k.txt"

LEVEL_ORDER = ["A1", "A2", "B1", "B2"]


def nfc(s):
    return unicodedata.normalize("NFC", s)


# ---------------------------------------------------------------- fetching

def fetch(url, filename, note=""):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, filename)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    print("      downloading %s%s" % (filename, note))
    req = urllib.request.Request(url, headers={"User-Agent": "german-vault-builder/1.0"})
    tmp = path + ".part"
    with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as out:
        shutil.copyfileobj(r, out, 1 << 20)
    os.replace(tmp, path)
    return path


def stream_kaikki(pos, wanted):
    """Stream a multi-hundred-MB kaikki file once; keep only what we need.

    Produces two small caches: the records for our headwords, and the complete
    headword list (our "is this a real German noun/verb?" gate).
    """
    recs_path = os.path.join(CACHE, "kaikki-%s.jsonl" % pos)
    lemmas_path = os.path.join(CACHE, "kaikki-%s-lemmas.txt" % pos)
    if os.path.exists(recs_path) and os.path.exists(lemmas_path):
        return recs_path, lemmas_path

    os.makedirs(CACHE, exist_ok=True)
    url = KAIKKI % {"p": pos}
    print("      streaming kaikki %s (large; filtered on the fly)" % pos)
    req = urllib.request.Request(url, headers={"User-Agent": "german-vault-builder/1.0"})
    kept = seen = 0
    lemmas = set()
    with urllib.request.urlopen(req, timeout=600) as r, \
            open(recs_path + ".part", "w", encoding="utf-8") as out:
        for raw in io.TextIOWrapper(r, "utf-8"):
            if not raw.strip():
                continue
            seen += 1
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            w = rec.get("word")
            if not w:
                continue
            lemmas.add(nfc(w))
            if nfc(w) in wanted:
                out.write(raw if raw.endswith("\n") else raw + "\n")
                kept += 1
            if seen % 50000 == 0:
                sys.stdout.write("\r        %s: %d records scanned, %d kept" % (pos, seen, kept))
                sys.stdout.flush()
    sys.stdout.write("\r        %s: %d records scanned, %d kept, %d lemmas\n"
                     % (pos, seen, kept, len(lemmas)))
    os.replace(recs_path + ".part", recs_path)
    with open(lemmas_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(sorted(lemmas)))
    return recs_path, lemmas_path


def load_charsplit():
    d = os.path.join(CACHE, "charsplit")
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "__init__.py"), "a").close()
    for f, note in (("splitter.py", ""), ("ngram_probs.json", " (40 MB)")):
        p = os.path.join(d, f)
        if not os.path.exists(p) or os.path.getsize(p) == 0:
            print("      downloading charsplit/%s%s" % (f, note))
            req = urllib.request.Request(CHARSPLIT % f,
                                         headers={"User-Agent": "german-vault-builder/1.0"})
            with urllib.request.urlopen(req, timeout=300) as r, open(p + ".part", "wb") as out:
                shutil.copyfileobj(r, out, 1 << 20)
            os.replace(p + ".part", p)
    if CACHE not in sys.path:
        sys.path.insert(0, CACHE)
    from charsplit.splitter import Splitter
    return Splitter()


# ---------------------------------------------------------------- morphology tables

SEPARABLE = [
    "aneinander", "auseinander", "gegenüber", "zusammen", "entgegen", "herunter",
    "hinunter", "zurecht", "zurück", "entlang", "voraus", "vorbei", "vorüber",
    "herauf", "heraus", "herbei", "herein", "hervor", "herum", "hinauf", "hinaus",
    "hinein", "hinweg", "weiter", "wieder", "empor", "nieder", "statt", "teil",
    "fest", "fort", "heim", "nach", "vor", "auf", "aus", "bei", "ein", "her",
    "hin", "los", "mit", "weg", "zu", "ab", "an",
]
INSEPARABLE = ["miss", "emp", "ent", "ver", "zer", "be", "er", "ge"]
VARIABLE = ["hinter", "durch", "über", "unter", "wider", "voll", "um"]
PREFIXES = SEPARABLE + INSEPARABLE + VARIABLE
PREFIX_KIND = {}
for _p in SEPARABLE:
    PREFIX_KIND[_p] = "separable"
for _p in INSEPARABLE:
    PREFIX_KIND[_p] = "inseparable"
for _p in VARIABLE:
    PREFIX_KIND[_p] = "variable"

# suffix -> how to rebuild the base lemma from the residue
SUFFIXES = [
    ("igkeit", ["", "ig"]), ("schaft", [""]), ("keit", [""]), ("heit", [""]),
    ("ismus", [""]), ("ität", [""]), ("ung", ["en", ""]), ("nis", ["en", ""]),
    ("tum", [""]), ("ling", ["en", ""]), ("chen", [""]), ("lein", [""]),
    ("lich", ["", "en"]), ("isch", [""]), ("bar", ["en", ""]), ("sam", ["en", ""]),
    ("haft", [""]), ("los", [""]), ("voll", [""]), ("ion", ["ieren"]),
    ("ist", [""]), ("erei", ["en"]), ("ei", ["en", ""]), ("er", ["en", ""]),
    ("in", [""]), ("ig", ["", "en"]), ("e", ["en", ""]),
]
SUFFIX_SET = {s for s, _ in SUFFIXES}
FUGEN = ["ens", "es", "en", "er", "s", "n", "e"]
UMLAUT = str.maketrans("äöüÄÖÜ", "aouAOU")

MORPHEME_NOTES = {
    "ver-": "away / wrongly / completion — often reverses or intensifies the base verb",
    "be-": "makes an intransitive verb transitive; applies the action *to* something",
    "er-": "achievement / coming into being — the action reaches its result",
    "ent-": "away from / undoing — removal or origination",
    "zer-": "to pieces — destructive",
    "ge-": "collective, or the past-participle marker",
    "miss-": "wrongly / badly",
    "emp-": "variant of ent- before f/p",
    "-ung": "verb → feminine noun naming the action or its result (die)",
    "-heit": "adjective → feminine abstract noun (die)",
    "-keit": "adjective → feminine abstract noun, after -ig/-lich/-bar (die)",
    "-igkeit": "adjective → feminine abstract noun (die)",
    "-schaft": "feminine noun of collectivity or state (die)",
    "-chen": "diminutive, always neuter, umlauts the stem (das)",
    "-lein": "diminutive, always neuter (das)",
    "-er": "agent or instrument noun, masculine (der)",
    "-in": "feminine agent noun (die)",
    "-lich": "adjective: -ly / -like",
    "-bar": "adjective: -able / -ible",
    "-los": "adjective: -less",
    "-sam": "adjective: tending to",
    "-haft": "adjective: having the quality of",
    "-isch": "adjective: pertaining to",
    "-ig": "adjective: characterised by",
    "-nis": "abstract noun (usually das or die)",
    "-tum": "abstract/collective noun, usually neuter (das)",
    "-ling": "masculine noun for a person characterised by the base (der)",
    "-ität": "feminine noun from a Latinate adjective (die)",
    "-ismus": "masculine noun, doctrine or system (der)",
    "-ion": "feminine noun from a Latinate verb (die)",
    "-ist": "masculine agent noun, adherent of an -ismus (der)",
    "-ei": "feminine noun: place of activity, or repeated activity (die)",
    "-erei": "feminine noun: repeated (often tiresome) activity (die)",
    "-e": "feminine deverbal or deadjectival noun (die)",
    "-voll": "adjective: full of",
    "-ist-": "",
}

VOWELS = re.compile(r"(ie|ei|au|äu|eu|[aeiouäöü])")


def stem_vowel(form):
    """First vowel nucleus after stripping a participle ge- / known prefix."""
    f = form
    for p in ("ge",) + tuple(sorted(PREFIXES, key=len, reverse=True)):
        if f.startswith(p) and len(f) - len(p) >= 3:
            f = f[len(p):]
            break
    m = VOWELS.search(f)
    return m.group(0) if m else "?"


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
    stem = nfc("".join(out)).strip().rstrip(". ")
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
        # casefold(), not lower(): macOS APFS and Windows NTFS are case-insensitive,
        # and APFS additionally folds ß -> ss, so Stem-Straßen.md and Stem-Strassen.md
        # are the *same file*. Fold the same way the filesystem does or notes get
        # silently overwritten and their inbound links dangle.
        return nfc(name).casefold()

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


# ---------------------------------------------------------------- CEFR loading

ANKI_JUNK = re.compile(r"\[sound:[^\]]*\]")
PAREN_TAG = re.compile(r"\((?:Sg|Pl|S\.g|P\.l)[^)]*\)", re.I)
HOMONYM = re.compile(r"\(\d+\)$")
ARTICLES = ("der", "die", "das")
GENDER_OF = {"der": "masculine", "die": "feminine", "das": "neuter"}


def clean_word(w):
    w = ANKI_JUNK.sub("", nfc(w)).strip()
    w = HOMONYM.sub("", w).strip()
    w = PAREN_TAG.sub("", w).strip()
    w = re.sub(r"\s*\(.*?\)\s*$", "", w).strip()
    return w.strip(" ,;")


def load_cefr():
    """-> {lemma: {level, gloss, article, plural, pos}}"""
    words = {}

    def add(word, level, gloss="", article="", plural="", pos="", ex_de="", ex_en=""):
        word = clean_word(word).strip("-")   # the lists carry stubs like "all-", "-zeug"
        if not word or len(word) > 40 or not re.match(r"^[A-Za-zÄÖÜäöüß][\wÄÖÜäöüß\- /]*$", word):
            return
        for variant in re.split(r"\s*/\s*", word):
            variant = variant.strip()
            if not variant or " " in variant:
                continue
            cur = words.get(variant)
            if cur is None:
                words[variant] = {"level": level, "gloss": gloss, "article": article,
                                  "plural": plural, "pos": pos,
                                  "ex_de": ex_de, "ex_en": ex_en}
            else:
                if LEVEL_ORDER.index(level) < LEVEL_ORDER.index(cur["level"]):
                    cur["level"] = level
                for k, v in (("gloss", gloss), ("article", article), ("plural", plural),
                             ("pos", pos), ("ex_de", ex_de), ("ex_en", ex_en)):
                    if v and not cur[k]:
                        cur[k] = v

    # A1: "die Abfahrt \t sentence \t translation"  (article embedded)
    for line in open(fetch(CEFR_SOURCES["A1"], "cefr-a1.tsv"), encoding="utf-8"):
        p = line.rstrip("\n").split("\t")
        if not p or not p[0].strip():
            continue
        w, art = p[0].strip(), ""
        first = w.split(" ")[0].lower()
        if first in ARTICLES and " " in w:
            art, w = first, w.split(" ", 1)[1]
        # NB: the A1 file has no word gloss at all — cols 1/2 are a sentence pair.
        # Feeding col 2 in as the definition yields "I have to work from tomorrow."
        # as the meaning of `ab`. Leave the gloss empty; Wiktionary fills it.
        add(w, "A1", "", art, pos="noun" if art else "",
            ex_de=p[1].strip() if len(p) > 1 else "",
            ex_en=p[2].strip() if len(p) > 2 else "")

    # A2: id, word, article, plural, _, _, de_sentence, en_word, en_sentence, audio, audio
    for line in open(fetch(CEFR_SOURCES["A2"], "cefr-a2.tsv"), encoding="utf-8"):
        p = line.rstrip("\n").split("\t")
        if len(p) < 9:
            continue
        art = p[2].strip().lower()
        add(p[1], "A2", p[7].strip(), art if art in ARTICLES else "",
            p[3].strip(), "noun" if art in ARTICLES else "",
            ex_de=p[6].strip(), ex_en=p[8].strip())

    # B1: header row; de_article, de_word_short, en_word, type
    b1 = [l.rstrip("\n").split("\t") for l in
          open(fetch(CEFR_SOURCES["B1"], "cefr-b1.tsv"), encoding="utf-8")]
    hdr = [h.strip() for h in b1[0]]
    idx = {h: i for i, h in enumerate(hdr)}
    for p in b1[1:]:
        if len(p) <= max(idx.get("en_word", 0), idx.get("de_word_short", 0)):
            continue
        art = p[idx["de_article"]].strip().lower()
        full = p[idx.get("de_word_full", 0)] if "de_word_full" in idx else ""
        plural = ""
        m = re.search(r",\s*(-\w*|[^\s,]+)\s*$", clean_word(full))
        if m and art:
            plural = m.group(1)
        add(p[idx["de_word_short"]], "B1", p[idx["en_word"]].strip(),
            art if art in ARTICLES else "", plural,
            p[idx["type"]].strip() if "type" in idx else "",
            ex_de=p[idx["de_sentence"]].strip() if "de_sentence" in idx else "",
            ex_en=p[idx["en_sentence"]].strip() if "en_sentence" in idx else "")

    # B2: "German,English"
    for i, line in enumerate(open(fetch(CEFR_SOURCES["B2"], "cefr-b2.csv"), encoding="utf-8")):
        if i == 0:
            continue
        p = line.rstrip("\n").split(",")
        if len(p) < 2:
            continue
        add(p[0], "B2", ",".join(p[1:]).strip())

    return words


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
    print("[1/7] fetching data sources")
    cefr = load_cefr()
    print("      %d CEFR headwords (A1-B2)" % len(cefr))

    wanted = set(cefr)
    wanted |= {w.capitalize() for w in cefr} | {w.lower() for w in cefr}
    noun_recs, noun_lem = stream_kaikki("noun", wanted)
    verb_recs, verb_lem = stream_kaikki("verb", wanted)

    NOUNS = {nfc(l.strip()) for l in open(noun_lem, encoding="utf-8") if l.strip()}
    VERBS = {nfc(l.strip()) for l in open(verb_lem, encoding="utf-8") if l.strip()}

    freq = {}
    for line in open(fetch(FREQ, "de_50k.txt"), encoding="utf-8"):
        p = line.split()
        if len(p) == 2:
            freq[nfc(p[0])] = int(p[1])

    splitter = load_charsplit()

    fams_zip = fetch(DERIVBASE, "derivbase-v2.0.zip")
    fam_base = {}
    with zipfile.ZipFile(fams_zip) as z:
        name = [n for n in z.namelist() if "families" in n][0]
        for line in io.TextIOWrapper(z.open(name), "utf-8"):
            members = [m for m in line.split() if "_" in m]
            if len(members) < 2 or len(members) > 60:
                continue
            parsed = [(m.rsplit("_", 1)[0], m.rsplit("_", 1)[1]) for m in members]
            base = min(parsed, key=lambda t: (0 if t[1] == "V" else 1, len(t[0]), t[0]))[0]
            for lemma, _ in parsed:
                fam_base.setdefault(nfc(lemma), nfc(base))
    print("      %d nouns / %d verbs in lexicon, %d frequency entries, %d derivational links"
          % (len(NOUNS), len(VERBS), len(freq), len(fam_base)))

    # ------------------------------------------------------------ lexicon gate
    def known(w):
        if not w or len(w) < 3:
            return False
        w = nfc(w)
        return (w in NOUNS or w in VERBS or w in cefr
                or w.capitalize() in NOUNS or w.lower() in VERBS
                or w.lower() in cefr or freq.get(w.lower(), 0) >= 500)

    def as_verb(w):
        return nfc(w) in VERBS or nfc(w).lower() in VERBS

    print("[2/7] loading Wiktionary records")
    lex = {}
    for path in (noun_recs, verb_recs):
        for line in open(path, encoding="utf-8"):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            w = nfc(rec.get("word", ""))
            if not w:
                continue
            slot = lex.setdefault(w, {"pos": rec.get("pos"), "gender": "", "plural": "",
                                      "glosses": [], "etym": [], "forms": []})
            for s in rec.get("senses", []):
                for g in s.get("glosses", [])[:1]:
                    if g not in slot["glosses"]:
                        slot["glosses"].append(g)
                for t in s.get("tags", []):
                    if t in ("masculine", "feminine", "neuter") and not slot["gender"]:
                        slot["gender"] = t
            for f in rec.get("forms", []):
                tags = f.get("tags", [])
                # "plural" also tags verb conjugations (stehen auf), so gate on POS
                if rec.get("pos") == "noun" and "plural" in tags \
                        and not ({"genitive", "dative", "accusative"} & set(tags)) \
                        and not slot["plural"]:
                    slot["plural"] = f.get("form", "")
                slot["forms"].append((f.get("form", ""), tuple(tags)))
            slot["etym"].extend(rec.get("etymology_templates", []))
    print("      %d Wiktionary entries matched" % len(lex))

    # ------------------------------------------------------------ decomposition
    print("[3/7] decomposing words into stems + morphemes")

    def strip_fugen(part):
        """Arbeits- -> Arbeit, Kranken- -> krank.

        Frequency-gated: without it `Haus` loses its Fugen-looking -s and becomes
        `Hau`, which is a real but rare word, so a pure lexicon check passes.
        """
        base = freq.get(part.lower(), 0)
        for f in FUGEN:
            if part.lower().endswith(f) and len(part) - len(f) >= 3:
                cand = part[: -len(f)]
                if (known(cand) or known(cand.capitalize())) \
                        and max(freq.get(cand.lower(), 0),
                                freq.get(cand.capitalize().lower(), 0)) > base:
                    return cand
        return part

    def singularize(w):
        """Straßen -> Straße, Massen -> Masse: a Fugen/plural form is not a stem.

        Gated on frequency, because Wiktionary lists plenty of rare nouns that
        happen to be a common noun minus its last letter (Haus -> Hau, Eis -> Ei).
        Only back off when the shorter form is the more common word.
        """
        if not w[:1].isupper():
            return w
        base = freq.get(w.lower(), 0)
        for f in FUGEN:
            if w.lower().endswith(f) and len(w) - len(f) >= 3:
                cand = nfc(w[: -len(f)])
                for c in (cand, cand.capitalize()):
                    if c in NOUNS and c.lower() != w.lower() \
                            and freq.get(c.lower(), 0) > base:
                        return c
        return w

    def canonical(w):
        """Pick the surface form the lexicon actually knows.

        Lowercase verbs win over capitalised nouns, otherwise every nominalised
        infinitive in Wiktionary (das Reißen, das Folgen) hijacks its verb stem.
        """
        w = singularize(nfc(w))
        if nfc(w) in cefr:
            return nfc(w)
        for c in (w.lower(), w):
            if nfc(c) in VERBS:
                return nfc(c)
        for c in (w, w.capitalize(), w.lower()):
            if nfc(c) in cefr or nfc(c) in NOUNS:
                return nfc(c)
        return nfc(w)

    def try_suffix(w):
        low = w.lower()
        for suf, rebuilds in SUFFIXES:
            if not low.endswith(suf) or len(w) - len(suf) < 3:
                continue
            res = w[: -len(suf)]
            for rb in rebuilds:
                for cand in (res + rb, (res + rb).translate(UMLAUT),
                             (res + rb).capitalize(), (res + rb).lower()):
                    if known(cand) and cand.lower() != w.lower():
                        return "-" + suf, canonical(cand)
        return None

    def try_prefix(w):
        low = w.lower()
        for pre in sorted(PREFIXES, key=len, reverse=True):
            if not low.startswith(pre) or len(w) - len(pre) < 3:
                continue
            res = w[len(pre):]
            for cand in (res, res.capitalize(), res.lower(), res + "en"):
                if known(cand) and (as_verb(cand) or cand.capitalize() in NOUNS):
                    return pre + "-", canonical(cand)
        return None

    def split_compound(w, depth=0):
        """Recursive, score-gated, lexicon-verified compound split."""
        if depth > 2 or len(w) < 9:
            return [w]
        try:
            cands = splitter.split_compound(w)
        except Exception:
            return [w]
        if not cands:
            return [w]
        score, head, tail = cands[0]
        if score < 0.7:
            return [w]
        head, tail = strip_fugen(head), strip_fugen(tail)
        if len(head) < 3 or len(tail) < 3:
            return [w]
        if head.lower() in PREFIXES or tail.lower() in SUFFIX_SET:
            return [w]          # that is affixation, not compounding
        if not (known(head) and known(tail)):
            return [w]
        return split_compound(head, depth + 1) + split_compound(tail, depth + 1)

    def analyse(word):
        stems, morphs = [], []
        for part in split_compound(word):
            cur, seen_forms = canonical(part), set()
            for _ in range(5):
                if cur.lower() in seen_forms:
                    break
                seen_forms.add(cur.lower())
                hit = try_suffix(cur)
                if hit:
                    morphs.append(hit[0]); cur = hit[1]; continue
                hit = try_prefix(cur)
                if hit:
                    morphs.append(hit[0]); cur = hit[1]; continue
                base = fam_base.get(cur) or fam_base.get(cur.capitalize()) \
                    or fam_base.get(cur.lower())
                if base and base.lower() != cur.lower() and len(base) < len(cur) + 3 \
                        and known(base):
                    cur = canonical(base); continue
                break
            if cur not in stems:
                stems.append(cur)
        return stems, [m for i, m in enumerate(morphs) if m not in morphs[:i]]

    vocab = {}
    for word, meta in sorted(cefr.items()):
        entry = lex.get(word) or lex.get(word.capitalize()) or {}
        gloss = meta["gloss"] or "; ".join(entry.get("glosses", [])[:2])
        gender = GENDER_OF.get(meta["article"], "") or entry.get("gender", "")
        article = meta["article"] or {v: k for k, v in GENDER_OF.items()}.get(gender, "")
        plural = entry.get("plural", "") or meta["plural"]
        pos = meta["pos"] or entry.get("pos", "")
        if not pos:
            pos = "noun" if (article or word[:1].isupper()) else (
                "verb" if word.endswith(("en", "ern", "eln")) and as_verb(word) else "other")
        stems, morphs = analyse(word)
        vocab[word] = {"word": word, "level": meta["level"], "gloss": gloss or "—",
                       "article": article, "gender": gender, "plural": plural,
                       "pos": pos, "stems": stems, "morphs": morphs,
                       "ex_de": meta.get("ex_de", ""), "ex_en": meta.get("ex_en", "")}

    decomposed = sum(1 for v in vocab.values()
                     if v["morphs"] or len(v["stems"]) > 1 or v["stems"][0] != v["word"])
    print("      %d words, %d have a non-trivial decomposition (%.0f%%)"
          % (len(vocab), decomposed, 100.0 * decomposed / len(vocab)))

    # ------------------------------------------------------------ ablaut
    print("[4/7] extracting strong-verb ablaut series")
    ablaut = {}
    for word, v in vocab.items():
        if v["pos"] != "verb" and not word.endswith(("en", "ern", "eln")):
            continue
        entry = lex.get(word)
        if not entry:
            continue
        pret = part = ""
        for form, tags in entry["forms"]:
            ts = set(tags)
            if not pret and "preterite" in ts and "singular" in ts \
                    and {"first-person", "third-person"} & ts and "subjunctive" not in ts:
                pret = form
            if not part and "participle" in ts and "past" in ts:
                part = form
        if not pret or not part or pret.endswith("te"):
            continue        # weak verb
        series = "%s–%s–%s" % (stem_vowel(word), stem_vowel(pret), stem_vowel(part))
        if "?" in series:
            continue
        ablaut.setdefault(series, []).append((word, pret, part))
    ablaut = {k: v for k, v in ablaut.items() if len(v) >= 2}
    print("      %d series covering %d strong verbs"
          % (len(ablaut), sum(len(v) for v in ablaut.values())))

    # ------------------------------------------------------------ graph
    print("[5/7] building the graph")
    stem_vocab = defaultdict(list)
    morph_vocab = defaultdict(list)
    morph_stems = defaultdict(set)
    stem_morphs = defaultdict(set)
    for word, v in vocab.items():
        for s in v["stems"]:
            stem_vocab[s].append(word)
        for m in v["morphs"]:
            morph_vocab[m].append(word)
            for s in v["stems"]:
                morph_stems[m].add(s)
                stem_morphs[s].add(m)

    stem_ablaut = {}
    for series, verbs in ablaut.items():
        for word, pret, part in verbs:
            for s in vocab[word]["stems"]:
                stem_ablaut.setdefault(s, set()).add(series)
    ablaut_stems = defaultdict(set)
    for s, series_set in stem_ablaut.items():
        for series in series_set:
            ablaut_stems[series].add(s)

    names = NameRegistry()
    for m in sorted(morph_vocab):
        names.assign(("M", m), "Morph", m)
    for s in sorted(stem_vocab):
        names.assign(("S", s), "Stem", s)
    for w in sorted(vocab):
        names.assign(("V", w), "Vocab", w)
    for a in sorted(ablaut_stems):
        names.assign(("A", a), "Ablaut", a)

    print("      %d morphemes, %d stems, %d vocabulary, %d ablaut series"
          % (len(morph_vocab), len(stem_vocab), len(vocab), len(ablaut_stems)))

    # ------------------------------------------------------------ write
    print("[6/7] writing notes")
    if os.path.isdir(VAULT):
        shutil.rmtree(VAULT)
    dirs = {d: os.path.join(VAULT, d) for d in
            ("Morphemes", "Stems", "Vocabulary", "Ablaut")}
    for d in dirs.values():
        os.makedirs(d)

    def write(folder, name, lines):
        path = os.path.join(dirs[folder], nfc(name) + ".md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines + ["", "---", ""]))

    def by_freq(w):
        return (-freq.get(w.lower(), 0), w)

    def by_level(w):
        return (LEVEL_ORDER.index(vocab[w]["level"]), -freq.get(w.lower(), 0), w)

    # --- Morphemes
    for m in sorted(morph_vocab):
        is_prefix = m.endswith("-")
        kind = (PREFIX_KIND.get(m.rstrip("-"), "prefix") + " prefix") if is_prefix \
            else "derivational suffix"
        tags = ["german/morpheme", "german/prefix" if is_prefix else "german/suffix"]
        if is_prefix:
            tags.append("german/" + PREFIX_KIND.get(m.rstrip("-"), "separable"))
        lines = [
            "---",
            "tags: [%s]" % ", ".join(tags),
            "---",
            "# Morpheme: %s" % m,
            "**Type:** %s" % kind,
            "**Function:** %s" % MORPHEME_NOTES.get(m, "—"),
            "",
            "### 🔗 Downstream Stems",
        ]
        lines += ["- [[%s]]" % names.by_key[("S", s)]
                  for s in sorted(morph_stems[m], key=by_freq)]
        lines += ["", "### 📚 Words"]
        lines += ["- [[%s]]" % names.by_key[("V", w)]
                  for w in sorted(set(morph_vocab[m]), key=by_level)]
        write("Morphemes", names.by_key[("M", m)], lines)

    # --- Stems
    for s in sorted(stem_vocab):
        entry = lex.get(s) or lex.get(s.capitalize()) or {}
        meaning = (cefr.get(s, {}).get("gloss") or
                   "; ".join(entry.get("glosses", [])[:2]) or "—")
        lvl = cefr.get(s, {}).get("level")
        tags = ["german/stem"] + (["cefr/%s" % lvl.lower()] if lvl else [])
        lines = [
            "---",
            "tags: [%s]" % ", ".join(tags),
            "---",
            "# Stem: %s" % s,
            "**Meaning:** %s" % meaning,
            "",
            "### 🧩 Combines with",
        ]
        ms = sorted(stem_morphs[s], key=lambda m: (not m.endswith("-"), m))
        lines += ["- [[%s]]" % names.by_key[("M", m)] for m in ms] or \
                 ["- *(appears bare or only in compounds)*"]
        if s in stem_ablaut:
            lines += ["", "### 🎵 Ablaut"]
            lines += ["- [[%s]]" % names.by_key[("A", a)] for a in sorted(stem_ablaut[s])]
        lines += ["", "### 🗣️ Vocabulary"]
        lines += ["- [[%s]]" % names.by_key[("V", w)]
                  for w in sorted(set(stem_vocab[s]), key=by_level)]
        write("Stems", names.by_key[("S", s)], lines)

    # --- Vocabulary
    for w, v in sorted(vocab.items()):
        tags = ["german/vocabulary", "cefr/%s" % v["level"].lower()]
        if v["pos"] in ("noun", "verb"):
            tags.append("german/" + v["pos"])
        lines = [
            "---",
            "tags: [%s]" % ", ".join(tags),
            "---",
            "# Vokabel: %s" % (("%s %s" % (v["article"], w)) if v["article"] else w),
        ]
        if v["article"] or v["gender"]:
            lines.append("**Article / Gender:** %s%s" % (
                v["article"] or "—",
                " (%s)" % v["gender"] if v["gender"] else ""))
        if v["plural"]:
            lines.append("**Plural:** %s" % v["plural"])
        lines.append("**Meaning:** %s" % v["gloss"])
        if v["ex_de"]:
            lines += ["", "### 📝 Example", "> %s" % v["ex_de"]]
            if v["ex_en"]:
                lines.append("> *%s*" % v["ex_en"])
        lines += ["", "### ⚙️ Stems"]
        lines += ["- [[%s]]" % names.by_key[("S", s)] for s in v["stems"]]
        lines += ["", "### 🧩 Morphemes"]
        lines += ["- [[%s]]" % names.by_key[("M", m)] for m in v["morphs"]] or \
                 ["- *(%s)*" % ("pure compound — no affixes" if len(v["stems"]) > 1
                                else "unanalysed — this word is its own stem")]
        write("Vocabulary", names.by_key[("V", w)], lines)

    # --- Ablaut
    for series in sorted(ablaut_stems):
        verbs = ablaut[series]
        lines = [
            "---",
            "tags: [german/ablaut, german/strong-verb]",
            "---",
            "# Ablaut: %s" % series,
            "**Pattern:** infinitive %s → preterite %s → participle %s"
            % tuple(series.split("–")),
            "",
            "### 🎵 Verbs in this series",
        ]
        for word, pret, part in sorted(verbs):
            lines.append("- **%s** → %s → %s" % (word, pret, part))
        lines += ["", "### 🔗 Stems"]
        lines += ["- [[%s]]" % names.by_key[("S", s)]
                  for s in sorted(ablaut_stems[series], key=by_freq)]
        write("Ablaut", names.by_key[("A", series)], lines)

    counts = {lv: sum(1 for v in vocab.values() if v["level"] == lv) for lv in LEVEL_ORDER}
    with open(os.path.join(VAULT, "_Vault Info.md"), "w", encoding="utf-8") as fh:
        fh.write(
            "---\ntags: [german/index]\n---\n"
            "# German Morpheme Graph Vault\n\n"
            "Morphemes → Stems → Vocabulary, plus an Ablaut layer for strong verbs.\n"
            "Where a kanji is a fixed node, a German **stem recombines**: `fahren` is the\n"
            "same node inside *erfahren*, *Erfahrung*, *abfahren* and *Fahrzeug*.\n\n"
            "| Layer | Notes |\n|---|---|\n"
            "| Morphemes | %d |\n| Stems | %d |\n| Vocabulary | %d |\n| Ablaut series | %d |\n\n"
            "Vocabulary by level: A1 %d · A2 %d · B1 %d · B2 %d.\n\n"
            "> Decomposition is etymological, not semantic — `verstehen` really is\n"
            "> *ver-* + *stehen*, the same way 泉 really is 白 + 水 without meaning\n"
            "> \"white water\".\n\n"
            "## Attribution\n"
            "- Word lists: Goethe-Institut A1–B2 (community transcriptions)\n"
            "- Definitions, gender, plural, principal parts: "
            "[kaikki.org](https://kaikki.org/dictionary/German/) / English Wiktionary "
            "(CC BY-SA 4.0, Ylonen LREC 2022)\n"
            "- Derivational families: DErivBase v2.0, IMS Stuttgart (Zeller et al., ACL 2013)\n"
            "- Compound splitting: [CharSplit](https://github.com/dtuggener/CharSplit) (MIT)\n"
            "- Frequency: [hermitdave/FrequencyWords](https://github.com/hermitdave/FrequencyWords) (MIT)\n"
            % (len(morph_vocab), len(stem_vocab), len(vocab), len(ablaut_stems),
               counts["A1"], counts["A2"], counts["B1"], counts["B2"])
        )

    # ------------------------------------------------------------ verify
    print("[7/7] verifying graph integrity")
    on_disk = set()
    for path in dirs.values():
        for f in os.listdir(path):
            if f.endswith(".md"):
                on_disk.add(nfc(f[:-3]))

    errors = []
    edges = set()
    link_re = re.compile(r"\[\[([^\]]+)\]\]")
    for path in dirs.values():
        for f in sorted(os.listdir(path)):
            if not f.endswith(".md"):
                continue
            src = nfc(f[:-3])
            text = nfc(open(os.path.join(path, f), encoding="utf-8").read())
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
