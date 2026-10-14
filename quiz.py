#!/usr/bin/env python3
"""Character -> meaning drill over a components/characters/vocabulary vault.

One engine for both Japanese_Kanji_Graph_Vault and Chinese_Hanzi_Graph_Vault:
the vault layouts are the same graph, so only a small profile differs (folder,
link prefix, level tag, vocab reading field), detected from which character
folder the vault has. Each vault keeps its own .quiz_state.json and its own
`known` tags.

Picks what to study from the vault's own graph rather than a frequency list:
a character is "cheap" if you already know words containing it, or if
learning it makes several otherwise-unreadable words readable. Correct
answers write the `known` tag back into the note's frontmatter, so the vault
stays the single source of truth and the frontier recomputes itself next run.

Quizzing is typed recall, graded loosely: you don't need the exact wording
in the note, just one real word in common with it (reflect/reflection both
count). A miss doesn't reveal the answer -- it offers components, vocabulary,
shared-component relatives, or your own mnemonic on request, so you keep
trying to recall instead of being told. One-stroke lookalikes like 未/末 or
士/土 decompose to themselves in the IDS data and share no component node;
find_mixup (a wrong guess that is another character's meaning) is what
catches those.

    python3 quiz.py --vault <vault>              # due reviews, then new cards
    python3 quiz.py --vault <vault> --new 30     # cap new cards this session
    python3 quiz.py --vault <vault> --max 50     # cap the whole session (default 30)
    python3 quiz.py --vault <vault> --plan       # show the queue, ask nothing
    python3 quiz.py --vault <vault> --confusable [N]   # drill N shared-component clusters
    python3 quiz.py --vault <vault> --lookalikes [N]   # drill N lookalike groups
    python3 quiz.py --vault <vault> --hard [N]         # re-study the N most-missed cards
    python3 quiz.py --vault <vault> --level n5-n4      # any mode, limited to these levels

Each vault has a launcher (quiz_kanji.py / quiz_hanzi.py) that fills in --vault.
"""

import argparse
import collections
import datetime
import json
import os
import random
import re
import sys
import unicodedata

PROFILES = {
    "Kanji": dict(prefix="Kanji-", noun="kanji", reading="Reading (Kana)",
                  level_re=r"jlpt/(n\d|unlisted)", level_fmt="{}",
                  levels=["n5", "n4", "n3", "n2", "n1", "unlisted"]),
    "Characters": dict(prefix="Hanzi-", noun="hanzi", reading="Pinyin",
                       level_re=r"hsk/(\d)", level_fmt="hsk{}",
                       levels=["1", "2", "3", "4", "5", "6"]),
}

VAULT = STATE = FOLDER = None
P = {}
LEVEL_ORDER = {}


def use_vault(path):
    global VAULT, STATE, FOLDER, P, LEVEL_ORDER
    VAULT = os.path.abspath(path)
    FOLDER = next((f for f in PROFILES if os.path.isdir(os.path.join(VAULT, f))), None)
    if FOLDER is None:
        sys.exit(f"{VAULT}: no {' or '.join(PROFILES)}/ folder -- not a quiz vault")
    P = PROFILES[FOLDER]
    STATE = os.path.join(VAULT, ".quiz_state.json")
    LEVEL_ORDER = {lvl: i for i, lvl in enumerate(P["levels"] + ["?"])}


MATURE = 21          # days; interval at which a card is tagged `known`
SESSION_NEW = 20
SESSION_MAX = 30     # hard cap on total questions -- a backlog day (many
                     # cards maturing to "due" on the same date) shouldn't
                     # turn into a 90-question session
SESSION_RESERVE = 5  # new cards guaranteed a slot even when reviews alone
                     # would fill the cap -- without it a standing backlog
                     # crowds new learning out entirely, every session

C = {"dim": "\033[2m", "b": "\033[1m", "g": "\033[32m", "y": "\033[33m",
     "r": "\033[31m", "c": "\033[36m", "0": "\033[0m"}
if not sys.stdout.isatty():
    C = {k: "" for k in C}


def nfc(s):
    return unicodedata.normalize("NFC", s)


# ---------------------------------------------------------------- vault I/O

def read_layer(folder, prefix):
    notes = {}
    d = os.path.join(VAULT, folder)
    for fn in os.listdir(d):
        if not fn.endswith(".md"):
            continue
        path = os.path.join(d, fn)
        text = open(path, encoding="utf-8").read()
        notes[nfc(fn[len(prefix):-3])] = {
            "path": path,
            "text": text,
            "known": is_known(text),
            "links": [nfc(l) for l in re.findall(r"\[\[([^\]]+)\]\]", text)],
        }
    return notes


def is_known(text):
    fm = frontmatter(text)
    return bool(re.search(r"(^|[\[,\s])known(\s*$|[\],\s])", fm, re.M))


def frontmatter(text):
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    return text[3:end] if end != -1 else ""


def meaning(text):
    m = re.search(r"^\*\*Meaning:\*\*\s*(.+)$", text, re.M)
    return m.group(1).strip() if m else "?"


ACCEPTED = "**Also accepted:**"


def accepted(text):
    m = re.search(r"^" + re.escape(ACCEPTED) + r"\s*(.+)$", text, re.M)
    return [a.strip() for a in m.group(1).split(",")] if m else []


ANCHOR = "**Anchor:**"   # [[Vocab-...]] override for the word a character is
                         # learned through; otherwise anchor_word() picks one
ALSO_MEANS = "**Also means:**"   # dictionary senses past the first three


def also_means(text):
    m = re.search(r"^" + re.escape(ALSO_MEANS) + r"\s*(.+)$", text, re.M)
    return m.group(1).strip() if m else ""


def answer_text(text):
    """What a guess is graded against: the gloss, the dictionary senses the
    gloss leaves out (直 'fix', 無 'not' are senses 4 and 6), and any wording
    you've overridden the grader with ('r'). Kept separate from meaning() so
    the short gloss is still what gets displayed."""
    return ", ".join(x for x in [meaning(text), also_means(text)] + accepted(text) if x)


def add_accepted(note, guess):
    """Stored as a **Also accepted:** line in the note's header field block,
    so it's visible and hand-editable in Obsidian like the mnemonic."""
    body = note["text"]
    have = accepted(body)
    if guess.lower() in (a.lower() for a in have):
        return
    line = f"{ACCEPTED} {', '.join(have + [guess])}\n"
    old = re.search(r"^" + re.escape(ACCEPTED) + r".*\n", body, re.M)
    if old:
        body = body[:old.start()] + line + body[old.end():]
    else:
        end = re.search(r"^# .*\n(?:\*\*[^\n]*\n)*", body, re.M).end()
        body = body[:end] + line + body[end:]
    open(note["path"], "w", encoding="utf-8").write(body)
    note["text"] = body


def level(text):
    m = re.search(P["level_re"], text)
    return m.group(1) if m else "?"


def level_label(text):
    lvl = level(text)
    return P["level_fmt"].format(lvl) if lvl != "?" else "?"


def reading_of(vocab_text):
    m = re.search(rf"^\*\*{re.escape(P['reading'])}:\*\*\s*(.+)$", vocab_text, re.M)
    return m.group(1).strip() if m else ""


def mark_known(note):
    """Add `known` to the note's tags, preserving whichever YAML style it uses.

    The vault ships inline `tags: [a, b]`, but Obsidian rewrites a note to a
    block list the moment you edit its tags by hand -- so both styles exist
    side by side and the writer has to match what it finds.
    """
    text = note["text"]
    if is_known(text):
        return
    inline = re.search(r"^tags:\s*\[([^\]]*)\]\s*$", text, re.M)
    if inline:
        new = f"tags: [{inline.group(1).strip()}, known]"
        text = text[:inline.start()] + new + text[inline.end():]
    else:
        # `-[ \t]+` with a mandatory space, not `-[ \t]*`: the closing `---`
        # otherwise parses as a list item whose value is `--`, and the tag
        # lands outside the frontmatter where Obsidian ignores it.
        block = re.search(r"^tags:[ \t]*\n((?:[ \t]*-[ \t]+\S+[ \t]*\n)+)",
                          text, re.M)
        if not block:
            return
        indent = re.match(r"([ \t]*)-", block.group(1)).group(1)
        text = (text[:block.end()] + f"{indent}- known\n" + text[block.end():])
    open(note["path"], "w", encoding="utf-8").write(text)
    note["text"] = text
    note["known"] = True


MNEMONIC_HEADING = "### 💡 Mnemonic"


SUGGESTED_HEADING = "### 🤖 Suggested mnemonic"   # a draft to rewrite in
                                                  # your own words, kept in
                                                  # its own section (placed
                                                  # before Components) so
                                                  # set_mnemonic never
                                                  # touches it


LOOKALIKES_HEADING = "### 👀 Lookalikes"   # links to the characters this one
                                           # gets mistaken for -- shape-based,
                                           # so it catches pairs that share no
                                           # component (未/末) as well


def get_section(text, heading):
    m = re.search(re.escape(heading) + r"\n(.*?)(?=\n### |\n---\s*$)", text, re.S)
    return m.group(1).strip() if m else None


def get_mnemonic(text):
    return get_section(text, MNEMONIC_HEADING)


def set_mnemonic(note, text):
    """A mnemonic you write yourself beats one handed to you -- the effort of
    making the connection is what encodes it. Stored in the note itself
    (right under the title's **Field:** lines) rather than in
    .quiz_state.json, so it's part of the vault proper: visible in Obsidian,
    searchable, survives independent of any SRS state, and re-editable by
    hand like any other note content. Anchored after the whole field block,
    not just Meaning -- hanzi notes carry a **Traditional:** line below it
    that would otherwise be swallowed into the replaced gap."""
    text = text.strip()
    body = note["text"]
    meaning_end = re.search(r"^# .*\n(?:\*\*[^\n]*\n)*", body, re.M).end()
    tail = body[meaning_end:]
    # the gap ends at the first heading that ISN'T the mnemonic heading --
    # searching for any "### " would stop at an existing mnemonic block
    # itself (it starts with "### " too) and insert a duplicate above it
    gap_end_rel = len(tail)
    for m in re.finditer(r"\n(### .+)\n", tail):
        if m.group(1) != MNEMONIC_HEADING:
            gap_end_rel = m.start()
            break
    gap_end = meaning_end + gap_end_rel
    body = body[:meaning_end] + f"\n{MNEMONIC_HEADING}\n{text}\n" + body[gap_end:]
    open(note["path"], "w", encoding="utf-8").write(body)
    note["text"] = body
    note["mnemonic"] = text


def add_lookalike(note, other):
    """Append a [[link]] to the note's Lookalikes section, creating it just
    before Components if needed. Used when a wrong guess turns out to be
    another character's meaning -- a confusion observed, not predicted."""
    if other in note["lookalikes"]:
        return
    body = note["text"]
    link = f"- [[{P['prefix']}{other}]]\n"
    sec = re.search(re.escape(LOOKALIKES_HEADING) + r"\n((?:- .*\n)*)", body)
    if sec:
        body = body[:sec.end()] + link + body[sec.end():]
    else:
        i = body.index("### 🧱 Components")
        body = body[:i] + f"{LOOKALIKES_HEADING}\n{link}\n" + body[i:]
    open(note["path"], "w", encoding="utf-8").write(body)
    note["text"] = body
    note["lookalikes"].append(other)


def anchor_word(k, d, vocab):
    """One word to learn the character through. Your hardest kanji are
    nearly all ones you know no word for (満 0/6, 験 0/5, 任 0/3), and their
    meanings blur within a family (検 'examination' vs 験 'verification');
    words don't blur that way -- 検査 is not 試験. Prefers an explicit
    **Anchor:** link, then a word you already know, then the most basic
    two-character compound; never the bare character itself."""
    m = re.search(r"^" + re.escape(ANCHOR) + r"\s*\[\[Vocab-([^\]]+)\]\]", d["text"], re.M)
    if m and nfc(m.group(1)) in vocab:
        return nfc(m.group(1))
    cands = [w for w in d["vocab"] if w in vocab and w != k]
    if not cands:
        return None
    return min(cands, key=lambda w: (not vocab[w]["known"],
                                     LEVEL_ORDER.get(level(vocab[w]["text"]), 99),
                                     len(w) != 2, len(w)))


def anchor_line(d, vocab):
    v = vocab[d["anchor"]]["text"]
    gloss = re.split(r"[;,]", meaning(v))[0].strip()
    return f"{d['anchor']}  {C['dim']}{reading_of(v)}{C['0']}  -- {gloss}"


# ---------------------------------------------------------------- frontier

def build():
    kanji = read_layer(FOLDER, P["prefix"])
    vocab = read_layer("Vocabulary", "Vocab-")
    cut = len(P["prefix"])
    for w, d in vocab.items():
        d["kanji"] = [l[cut:] for l in d["links"] if l.startswith(P["prefix"])]
    for k, d in kanji.items():
        d["vocab"] = [l[6:] for l in d["links"] if l.startswith("Vocab-")]
        d["comps"] = [l[5:] for l in d["links"] if l.startswith("Comp-")]
        d["mnemonic"] = get_mnemonic(d["text"])
        d["suggested"] = get_section(d["text"], SUGGESTED_HEADING)
        look = get_section(d["text"], LOOKALIKES_HEADING) or ""
        d["lookalikes"] = [nfc(x) for x in re.findall(r"\[\[" + re.escape(P["prefix"])
                                                      + r"([^\]]+)\]\]", look)]
    for k, d in kanji.items():
        d["lookalikes"] = [x for x in d["lookalikes"] if x in kanji]
        d["anchor"] = anchor_word(k, d, vocab)

    known_v = {w for w, d in vocab.items() if d["known"]}
    known_k = {k for k, d in kanji.items() if d["known"]}

    for k, d in kanji.items():
        d["seen_in"] = [w for w in d["vocab"] if w in known_v]
        d["half"] = len(d["seen_in"])
        d["unlocks"] = 0
    for w, d in vocab.items():
        if w in known_v:
            continue
        unk = [k for k in d["kanji"] if k not in known_k]
        if len(unk) == 1:
            kanji[unk[0]]["unlocks"] += 1
    return kanji, vocab


def build_comp_index(kanji):
    idx = collections.defaultdict(set)
    for k, d in kanji.items():
        for c in d["comps"]:
            idx[c].add(k)
    return idx


def load_component_meanings():
    """A component hint is only useful if it lets you build the answer
    yourself -- bare characters like 兑 in 説's breakdown are meaningless on
    their own. Some components fall back to a Unicode codepoint name (no
    curated meaning exists), which is honest but not a real mnemonic lever;
    those print as-is rather than being hidden."""
    out = {}
    for fn in os.listdir(os.path.join(VAULT, "Components")):
        if not fn.endswith(".md"):
            continue
        text = open(os.path.join(VAULT, "Components", fn), encoding="utf-8").read()
        out[nfc(fn[len("Comp-"):-3])] = meaning(text)
    return out


SIMILAR_SHOWN_PER_GROUP = 5   # per component, not a global cap -- a common
                              # radical still gets a (truncated) row instead
                              # of being silently excluded entirely


def similar_by_component(k, d, kanji, comp_idx):
    """Every component k has, each as its own group -- not just the rarest.
    Anchoring only on the rarest component (映's old behaviour: only ever
    show {英, 央} via 央, never anything via the common 日) assumes the
    confusable pair is always the tight, rare-component family. It isn't
    always: someone who fixates on a common radical (説's 言, shared by 51
    kanji) and under-reads the rest will mix up kanji via THAT component,
    and a rare-only anchor can never surface them. Grouped and sorted
    rarest-first so the tight family still leads, but the common-radical
    crowd is no longer invisible -- just truncated."""
    dcomps = set(d["comps"])
    groups = []
    for c in d["comps"]:
        # within a group, members sharing MORE components with k rank first --
        # true visual similarity isn't computable here, but extra structural
        # overlap is the best proxy available, and it's what keeps a member
        # sharing two components from being buried behind one sharing only
        # this one, alphabetically, in a large group's truncated display
        members = sorted((k2 for k2 in comp_idx[c] if k2 != k),
                         key=lambda k2: (-len(dcomps & set(kanji[k2]["comps"])), k2))
        if members:
            groups.append((c, len(comp_idx[c]), members))
    groups.sort(key=lambda g: g[1])
    return groups


MAX_CLUSTER_SIZE = 5   # a dedicated drill needs a genuine lookalike pair,
                       # not a whole radical family like the 51-member
                       # 言-based group -- unlike the reactive "s" hint
                       # (similar_by_component), which shows large groups
                       # too, just truncated, this one excludes them outright


def build_clusters(kanji, comp_idx):
    """Every component whose group is small enough to be a real lookalike
    set rather than a radical class, collapsed to unique member-sets so a
    kanji with two rare components doesn't spawn the same cluster twice."""
    seen, clusters = set(), []
    for c, members in comp_idx.items():
        if not (2 <= len(members) <= MAX_CLUSTER_SIZE):
            continue
        key = frozenset(members)
        if key in seen:
            continue
        seen.add(key)
        clusters.append((c, sorted(members)))
    return clusters


def rank_clusters(clusters, kanji):
    """Clusters worth drilling: at least one member still unknown, most
    unknowns first so a session front-loads the ones that'll actually move
    the frontier."""
    scored = []
    for anchor, members in clusters:
        unknown = sum(1 for m in members if not kanji[m]["known"])
        if unknown:
            scored.append((anchor, members, unknown))
    scored.sort(key=lambda t: (-t[2], len(t[1])))
    return scored


def frontier(kanji):
    """Cheapest-first. Kanji hiding inside words you already know come first --
    they cost almost nothing, and they're where your tagging has drifted. Then
    the ones that unlock the most currently-unreadable words."""
    pool = [(k, d) for k, d in kanji.items() if not d["known"]]

    def rank(item):
        k, d = item
        if d["half"]:
            return (0, -d["half"], -d["unlocks"])
        if d["unlocks"]:
            return (1, -d["unlocks"], LEVEL_ORDER[level(d["text"])])
        return (2, LEVEL_ORDER[level(d["text"])], -len(d["vocab"]))

    return [k for k, _ in sorted(pool, key=rank)]


# ---------------------------------------------------------------- scheduling

def today():
    return datetime.date.today().toordinal()


def load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE, encoding="utf-8"))
    return {}


def save_state(state):
    json.dump(state, open(STATE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, sort_keys=True)


HARD_LAPSES = 3          # misses before a card counts as hard
HARD_STEPS = [1, 3, 7]   # days after each correct answer while relearning a
                         # hard card -- SM-2's 1 -> 6 -> ~15 jump lets it
                         # slip again before it has settled


def is_hard(card):
    return card.get("lapses", 0) >= HARD_LAPSES


def schedule(card, grade):
    """SM-2, lightly trimmed. grade: 1 again, 2 hard, 3 good, 4 easy. Misses
    are counted (lapses); a hard card climbs back through HARD_STEPS before
    normal growth resumes."""
    ease = card.get("ease", 2.5)
    reps = card.get("reps", 0)
    ivl = card.get("ivl", 0)
    lapses = card.get("lapses", 0)
    if grade == 1:
        reps, ivl, ease = 0, 0, max(1.3, ease - 0.2)
        lapses += 1
    else:
        stepping = lapses >= HARD_LAPSES and reps < len(HARD_STEPS)
        if stepping:
            ivl = HARD_STEPS[reps]
        elif reps == 0:
            ivl = 1
        elif reps == 1:
            ivl = 6
        else:
            ivl = max(1, round(ivl * (ease if grade >= 3 else 1.2)))
        if grade == 2:
            ease = max(1.3, ease - 0.15)
        elif grade == 4:
            if not stepping:
                ivl = round(ivl * 1.3)
            ease += 0.15
        reps += 1
    card.update(ease=round(ease, 2), reps=reps, ivl=ivl, lapses=lapses, due=today() + ivl)
    return card


# ---------------------------------------------------------------- session

STOPWORDS = {"a", "an", "the", "to", "of", "or", "and", "for", "with",
            "this", "that", "one", "some", "etc"}


def singular(w):
    # duties/duty would otherwise fail even the 4-letter prefix match
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


def significant_words(s):
    return [singular(w) for w in re.findall(r"[a-zA-Z']+", s.lower())
           if len(w) > 2 and w not in STOPWORDS]


def is_match(guess, answer):
    """One real word in common counts -- this isn't graded on wording, only
    on whether you actually recalled the concept. A length-4 prefix match
    (reflect/reflection) covers the common inflection gap without a stemmer."""
    g = significant_words(guess)
    if not g:
        return False
    a = significant_words(answer)
    aset = set(a)
    for w in g:
        if w in aset:
            return True
        if len(w) >= 4 and any(len(w2) >= 4 and w[:4] == w2[:4] for w2 in a):
            return True
    return False


MEANING_WORD_CAP = 5   # a word shared by more kanji than this ("counter",
                       # "get", "time"...) is template noise, not a real
                       # cross-kanji signal


def build_meaning_index(kanji):
    idx = collections.defaultdict(set)
    for k, d in kanji.items():
        for w in significant_words(answer_text(d["text"])):
            idx[w].add(k)
    return idx


def find_mixup(guess, k, kanji, meaning_idx, n=3):
    """A wrong guess that's actually SOMEONE ELSE's meaning is the strongest
    possible signal of what you're really confusing this kanji with -- it's
    discovered from the mistake itself, not guessed at from shared shape.
    Generic words are excluded via MEANING_WORD_CAP so 'counter' (24 kanji)
    doesn't turn every wrong guess about a counter-word into a false hit."""
    hits = collections.Counter()
    for w in significant_words(guess):
        ks = meaning_idx.get(w, ())
        if 0 < len(ks) <= MEANING_WORD_CAP:
            for k2 in ks:
                if k2 != k:
                    hits[k2] += 1
    return [k2 for k2, _ in sorted(hits.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]


class _Quit(Exception):
    pass


def log_attempt(k, events, outcome, grade=None):
    """One JSON line per question in .quiz_log.jsonl: every guess and lookup
    in order, and how it ended. The SRS state only keeps ease/interval, which
    says THAT a kanji keeps failing but not WHY -- blank, wrong concept,
    mixed up with another character, or right idea in words the grader
    rejected. This is what makes that diagnosable."""
    if not events and outcome == "quit":
        return
    entry = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
             "char": k, "events": events, "outcome": outcome, "grade": grade}
    with open(os.path.join(VAULT, ".quiz_log.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _read(msg):
    try:
        g = input(msg).strip()
    except EOFError:
        raise _Quit
    if g.lower() == "q":
        raise _Quit
    return g


def show_vocab_hint(d, vocab):
    if not d["vocab"]:
        print(f"  {C['dim']}no vocabulary linked to this {P['noun']}{C['0']}")
        return
    # known words first (the actual memory-jog), but the rest are shown too
    # rather than hidden -- the full picture of how this kanji gets used
    words = sorted(d["vocab"], key=lambda w: not vocab[w]["known"])
    print(f"  {C['y']}vocabulary:{C['0']}")
    for w in words[:8]:
        tag = (f"{C['g']}known{C['0']}" if vocab[w]["known"]
              else f"{C['dim']}unknown{C['0']}")
        print(f"    {w}  {C['dim']}{reading_of(vocab[w]['text'])}{C['0']}  {tag}")
    extra = len(words) - 8
    if extra > 0:
        print(f"    {C['dim']}+{extra} more{C['0']}")


def show_components(d, comp_meanings):
    if not d["comps"]:
        print(f"  {C['dim']}(no components recorded){C['0']}")
        return
    print(f"  {C['y']}components:{C['0']}")
    for c in d["comps"]:
        print(f"    {c}  ({comp_meanings.get(c, '?').split(',')[0].strip()})")


def short(text):
    return meaning(text).split(",")[0].strip()


def show_similar(k, d, kanji, comp_idx):
    if d["lookalikes"]:
        print(f"  {C['y']}looks like:{C['0']}")
        for k2 in d["lookalikes"]:
            print(f"    {k2}  ({short(kanji[k2]['text'])})")
    groups = similar_by_component(k, d, kanji, comp_idx)
    if not groups:
        if not d["lookalikes"]:
            print(f"  {C['dim']}no shared-component relatives in this vault{C['0']}")
        return
    print(f"  {C['y']}shares a component with:{C['0']}")
    for c, total, members in groups:
        shown = members[:SIMILAR_SHOWN_PER_GROUP]
        print(f"    via {c} ({total} {P['noun']}):")
        for k2 in shown:
            print(f"      {k2}  ({meaning(kanji[k2]['text']).split(',')[0].strip()})")
        extra = total - 1 - len(shown)
        if extra > 0:
            print(f"      {C['dim']}+{extra} more{C['0']}")


def menu(lead, options):
    row = " · ".join(f"{key} {label}" for key, label in options)
    return f"{C['dim']}  {lead} · {row}{C['0']}  "


FIRST_MENU = menu("type the meaning", [
    ("c", "components"), ("v", "vocab"), ("s", "similar"),
    ("m", "mnemonic"), ("w", "word"), ("q", "quit"),
])
RETRY_MENU = menu("try again", [
    ("c", "components"), ("v", "vocab"), ("s", "similar"),
    ("m", "mnemonic"), ("w", "word"), ("r", "I was right"), ("g", "give up"),
])


STRAY_KEYS = {"c", "v", "s", "m", "w", "r", "g", "q", "k", "h"}   # every letter that means
                                                   # something elsewhere in
                                                   # the menus -- typed here
                                                   # out of habit, not content


def save_mnemonic_if_real(d, text, verb):
    if text.strip().lower() in STRAY_KEYS:
        print(f"  {C['dim']}(not saved -- looked like a stray menu key, "
             f"not a mnemonic){C['0']}")
        return
    set_mnemonic(d, text)
    print(f"  {C['g']}{verb}{C['0']}")


def show_anchor(d, vocab):
    if d["anchor"]:
        print(f"  {C['y']}as in:{C['0']} {anchor_line(d, vocab)}")
    else:
        print(f"  {C['dim']}no word in the vault to anchor this one{C['0']}")


def show_suggested(d):
    print(f"  {C['y']}suggested mnemonic:{C['0']} {d['suggested']}")


def write_prompt(d, lead):
    """Rewriting the draft in your own words is the point -- reading a
    mnemonic someone else made sticks less than one you put together."""
    if d["suggested"]:
        show_suggested(d)
        return _read(f"{C['dim']}  {lead} -- rewrite it in your own words "
                     f"(blank to skip){C['0']}  ")
    return _read(f"{C['dim']}  {lead} (blank to skip){C['0']}  ")


def do_mnemonic(k, d):
    if d["mnemonic"]:
        print(f"  {C['y']}your mnemonic:{C['0']} {d['mnemonic']}")
        if d["suggested"]:
            show_suggested(d)
        edit = _read(f"{C['dim']}  enter to keep it, or type a replacement{C['0']}  ")
        if edit:
            save_mnemonic_if_real(d, edit, "updated")
    else:
        note = write_prompt(d, "write a mnemonic")
        if note:
            save_mnemonic_if_real(d, note, "saved")


def study(k, d, ctx, n, total, label="new"):
    """First sight of a character is a study card, not a test. Testing a
    character you've never seen can only end in a give-up, and the SRS
    scores that as a lapse -- a permanent ease penalty for something never
    learned. Nothing is graded here; the real test comes a few cards later.
    Returns False on quit."""
    kanji, vocab, comp_meanings = ctx["kanji"], ctx["vocab"], ctx["comp_meanings"]
    print(f"\n{C['dim']}{'─' * 52}{C['0']}")
    print(f"{C['dim']}{n}/{total}   {level_label(d['text'])}   {C['0']}{C['y']}{label}{C['0']}")
    print(f"\n      {C['b']}{C['c']}{k}{C['0']}   {C['g']}{meaning(d['text'])}{C['0']}\n")
    if also_means(d["text"]):
        print(f"  {C['dim']}also: {also_means(d['text'])}{C['0']}")
    if d["anchor"]:
        show_anchor(d, vocab)
    show_components(d, comp_meanings)
    if d["mnemonic"]:
        print(f"  {C['y']}your mnemonic:{C['0']} {d['mnemonic']}")
    elif d["suggested"]:
        show_suggested(d)
    if d["vocab"]:
        show_vocab_hint(d, vocab)
    if d["lookalikes"]:
        print(f"  {C['y']}don't confuse with:{C['0']}")
        for k2 in d["lookalikes"]:
            print(f"    {k2}  ({short(kanji[k2]['text'])})")
    try:
        _read(f"{C['dim']}  study it, then enter -- you'll be tested in a few cards{C['0']}  ")
    except _Quit:
        return False
    log_attempt(k, [["study"]], "studied")
    return True


def ask(k, d, ctx, n, total):
    """The lookups on offer are the same from the very first prompt as after
    a miss -- c/v/s just loop back for another guess, no separate one-shot
    hint step. Every wrong guess is checked against every OTHER kanji's
    meaning (find_mixup): if you typed someone else's answer, that's the
    real mix-up, discovered from the mistake itself rather than guessed at
    from shared shape. A saved mnemonic also surfaces on the very first miss
    (cheapest, most tailored cue there is); with none yet, two blind misses
    force a generic vocab/component hint, and a third forces the
    shared-component list, rather than let repeated unaided wrong guesses
    rehearse the wrong association. Giving up with no mnemonic saved offers
    to write one right then -- the struggle is freshest exactly when you've
    just seen the answer. Only a fully cold, no-aid, first-try correct
    answer counts as "easy"; anything that took a lookup or a wrong attempt
    is "good"; only an explicit give-up is "again". 'r' overrides the grader
    for the last rejected guess (right idea, wording not in the gloss): it
    counts as correct at the grade that guess would have earned, and the
    wording is saved as an accepted answer for next time."""
    vocab, kanji, comp_idx = ctx["vocab"], ctx["kanji"], ctx["comp_idx"]
    comp_meanings, meaning_idx = ctx["comp_meanings"], ctx["meaning_idx"]
    print(f"\n{C['dim']}{'─' * 52}{C['0']}")
    print(f"{C['dim']}{n}/{total}   {level_label(d['text'])}{C['0']}")
    print(f"\n      {C['b']}{C['c']}{k}{C['0']}\n")
    events = []
    try:
        guess = _read(FIRST_MENU)
        cold = True
        answer = meaning(d["text"])
        misses = 0
        last_wrong, last_wrong_cold = None, False
        while True:
            low = guess.lower()
            if low in ("c", "v", "s", "m", "w"):
                cold = False
                events.append(["hint", low])
                if low == "c":
                    show_components(d, comp_meanings)
                elif low == "v":
                    show_vocab_hint(d, vocab)
                elif low == "s":
                    show_similar(k, d, kanji, comp_idx)
                elif low == "w":
                    show_anchor(d, vocab)
                else:
                    do_mnemonic(k, d)
            elif low == "r":
                if last_wrong is None:
                    print(f"  {C['dim']}nothing to override -- type a guess first{C['0']}")
                else:
                    add_accepted(d, last_wrong)
                    print(f"  {C['g']}✓ counted -- \"{last_wrong}\" now accepted "
                         f"for {k}{C['0']}  {C['dim']}({answer}){C['0']}")
                    grade = 4 if last_wrong_cold else 3
                    events.append(["override", last_wrong])
                    log_attempt(k, events, "override", grade)
                    break
            elif not guess or low == "g":
                events.append(["give_up"])
                print(f"  {C['r']}✗ {k} means:{C['0']} {answer}")
                if d["anchor"]:
                    show_anchor(d, vocab)
                grade = 1
                if d["unlocks"]:
                    print(f"  {C['dim']}unlocks {d['unlocks']} more words{C['0']}")
                if d["mnemonic"]:
                    print(f"  {C['y']}your mnemonic:{C['0']} {d['mnemonic']}")
                    _read(f"{C['dim']}  enter for next{C['0']}  ")
                else:
                    note = write_prompt(d, "write a mnemonic for next time")
                    if note:
                        save_mnemonic_if_real(d, note, "saved")
                log_attempt(k, events, "gave_up", grade)
                return grade
            elif is_match(guess, answer_text(d["text"])):
                events.append(["guess", guess])
                print(f"  {C['g']}✓ {answer}{C['0']}")
                grade = 4 if cold else 3
                log_attempt(k, events, "correct", grade)
                break
            else:
                last_wrong, last_wrong_cold = guess, cold
                cold = False
                misses += 1
                events.append(["guess", guess])
                print(f'  {C["r"]}✗ not quite ("{guess}"){C["0"]}')
                mixups = find_mixup(guess, k, kanji, meaning_idx)
                if mixups:
                    events.append(["mixup", mixups])
                    print(f"  {C['y']}that's the meaning of:{C['0']}")
                    for k2 in mixups:
                        print(f"    {k2}  ({meaning(kanji[k2]['text'])})")
                    print(f"  {C['dim']}maybe that's what you're thinking of{C['0']}")
                    # only shape-related confusions become lookalikes: a
                    # candidate that merely shares a meaning word (険 'deep'
                    # -> 深) is a meaning mix-up, not something that looks alike
                    for k2 in mixups:
                        if (k2 not in d["lookalikes"]
                                and set(d["comps"]) & set(kanji[k2]["comps"])):
                            add_lookalike(d, k2)
                            add_lookalike(kanji[k2], k)
                            print(f"  {C['dim']}{k} and {k2} added to each other's "
                                 f"lookalikes{C['0']}")
                # repeated blind guessing trains the wrong association -- force
                # a real clue in rather than let it loop unaided. your own
                # mnemonic (if any) is the cheapest, most tailored one, so it
                # surfaces before the generic vocab/component/similar aids
                if misses == 1 and d["anchor"] and is_hard(ctx["state"].get(k, {})):
                    events.append(["auto_hint", "w"])
                    show_anchor(d, vocab)
                elif misses == 1 and d["mnemonic"]:
                    events.append(["auto_hint", "m"])
                    print(f"  {C['y']}your mnemonic:{C['0']} {d['mnemonic']}")
                elif misses == 1 and d["suggested"]:
                    events.append(["auto_hint", "suggested"])
                    show_suggested(d)
                elif misses == 2:
                    events.append(["auto_hint", "v" if d["vocab"] else "c"])
                    print(f"  {C['y']}two misses -- here's a hint:{C['0']}")
                    (show_vocab_hint(d, vocab) if d["vocab"]
                    else show_components(d, comp_meanings))
                elif misses >= 3:
                    events.append(["auto_hint", "s"])
                    print(f"  {C['y']}still stuck -- visual family:{C['0']}")
                    show_similar(k, d, kanji, comp_idx)
            guess = _read(RETRY_MENU)
    except _Quit:
        log_attempt(k, events, "quit")
        return None

    if d["unlocks"]:
        print(f"  {C['dim']}unlocks {d['unlocks']} more words{C['0']}")
    return grade


def drill_groups(ctx, state, groups, label):
    """Each group is quizzed back-to-back in shuffled order, then shown side
    by side with its parts -- discrimination is the point, so the contrast
    has to be felt in the moment, and the difference is the last thing you
    see, not encountered accidentally weeks apart."""
    kanji, comp_meanings = ctx["kanji"], ctx["comp_meanings"]
    graded = tagged = 0
    for title, members in groups:
        print(f"\n{C['b']}{C['c']}{title}{C['0']}")
        unseen = [k for k in members if k not in state]
        for i, k in enumerate(unseen, 1):
            if not study(k, kanji[k], ctx, i, len(unseen)):
                save_state(state)
                return
        order = members[:]
        random.shuffle(order)
        for i, k in enumerate(order, 1):
            result = ask(k, kanji[k], ctx, i, len(order))
            if result is None:
                save_state(state)
                print(f"\n{graded} graded so far")
                return
            card = schedule(state.get(k, {}), result)
            state[k] = card
            graded += 1
            if card["ivl"] >= MATURE:
                mark_known(kanji[k])
                tagged += 1
                print(f"  {C['g']}matured → tagged known{C['0']}")
            else:
                print(f"  {C['dim']}next in {card['ivl']}d{C['0']}")

        try:
            reverse_round(members, ctx)
        except _Quit:
            save_state(state)
            return

        print(f"\n  {C['y']}side by side:{C['0']}")
        for k in members:
            parts = "  ".join(f"{c}({comp_meanings.get(c, '?').split(',')[0].strip()})"
                              for c in kanji[k]["comps"])
            tag = (f"{C['g']}known{C['0']}" if kanji[k]["known"]
                  else f"{C['dim']}unknown{C['0']}")
            print(f"    {k}  {short(kanji[k]['text']):18}  {parts:34}  {tag}")

    save_state(state)
    print(f"\n{C['dim']}{'─' * 52}{C['0']}")
    print(f"{graded} graded across {len(groups)} {label} · {tagged} newly tagged known")


def reverse_round(members, ctx):
    """The other direction: given a meaning, pick which of the group it is.
    Recalling each meaning on its own isn't the hard part for a family like
    険 検 験 剣 -- keeping straight which part goes with which meaning is,
    and this drills exactly that mapping. Practice only: not scheduled, but
    logged."""
    kanji, comp_meanings = ctx["kanji"], ctx["comp_meanings"]
    senses = {k: [s.strip() for s in meaning(kanji[k]["text"]).split(",")] for k in members}
    cue = {k: senses[k][0] for k in members}
    for k in members:
        if list(cue.values()).count(cue[k]) > 1:
            cue[k] = ", ".join(senses[k][:2])
    opts = "  ".join(f"{i}) {k}" for i, k in enumerate(members, 1))
    print(f"\n  {C['y']}now the other way -- which one is it?{C['0']}")
    right = 0
    order = members[:]
    random.shuffle(order)
    for k in order:
        ans = _read(f"  {C['b']}{cue[k]}{C['0']}   {C['dim']}{opts}{C['0']}  ")
        pick = (members[int(ans) - 1] if ans.isdigit() and 1 <= int(ans) <= len(members)
                else ans.strip()[:1])
        if pick == k:
            right += 1
            print(f"  {C['g']}✓ {k}{C['0']}")
            log_attempt(k, [["reverse", pick]], "reverse_ok")
        else:
            parts = "  ".join(f"{c}({comp_meanings.get(c, '?').split(',')[0].strip()})"
                              for c in kanji[k]["comps"])
            print(f"  {C['r']}✗ it's {k}{C['0']}  {C['dim']}{parts}{C['0']}")
            log_attempt(k, [["reverse", pick]], "reverse_miss")
    print(f"  {C['dim']}{right}/{len(order)} the other way{C['0']}")


def hard_session(ctx, state, n, levels=None):
    """Cards that have failed HARD_LAPSES+ times. Repeating them as they are
    hasn't worked, so each is re-studied (anchor word first), you're asked
    to rewrite the mnemonic that didn't stick, then all are tested with the
    usual same-session retry. Their schedule climbs back through HARD_STEPS
    (1, 3, 7 days) before normal growth resumes."""
    kanji = ctx["kanji"]
    cards = sorted((k for k, c in state.items()
                    if k in kanji and is_hard(c) and not kanji[k]["known"]
                    and in_levels(kanji, [k], levels)),
                   key=lambda k: (-state[k]["lapses"], state[k]["ease"]))[:n]
    if not cards:
        print(f"no hard cards (missed {HARD_LAPSES}+ times) right now")
        return
    print(f"\n{C['b']}{C['c']}{len(cards)} hard cards{C['0']}  "
          f"{C['dim']}re-study each, rewrite what didn't stick, then the test{C['0']}")
    for i, k in enumerate(cards, 1):
        d = kanji[k]
        print(f"\n{C['dim']}missed {state[k]['lapses']} times{C['0']}", end="")
        if not study(k, d, ctx, i, len(cards), label="re-study"):
            save_state(state)
            return
        try:
            lead = ("this mnemonic hasn't been working -- write a new one, built on the word "
                    "or the parts (blank keeps it)" if d["mnemonic"]
                    else "write a mnemonic, built on the word or the parts (blank to skip)")
            note = _read(f"{C['dim']}  {lead}{C['0']}  ")
        except _Quit:
            save_state(state)
            return
        if note:
            save_mnemonic_if_real(d, note, "saved")
    queue = cards[:]
    random.shuffle(queue)
    graded, tagged = run_queue(ctx, state, queue)
    save_state(state)
    left = sum(1 for k, c in state.items() if k in kanji and is_hard(c) and not kanji[k]["known"])
    print(f"\n{C['dim']}{'─' * 52}{C['0']}")
    print(f"{graded} graded · {tagged} newly tagged known · {left} hard cards in total")


def confusable_session(ctx, state, n_clusters, levels=None):
    kanji, comp_idx = ctx["kanji"], ctx["comp_idx"]
    clusters = [c for c in rank_clusters(build_clusters(kanji, comp_idx), kanji)
                if in_levels(kanji, c[1], levels)][:n_clusters]
    if not clusters:
        print("no confusable clusters need drilling right now")
        return
    drill_groups(ctx, state, [
        (f"confusable group -- shares {anchor}  ({unknown} unknown of {len(members)})",
         members) for anchor, members, unknown in clusters], "clusters")


def lookalike_groups(kanji, state, n, levels=None):
    """A character plus the ones listed under its Lookalikes, worst first:
    still-unknown before known, then lowest ease, then never-studied. A
    character joins at most one group per session so the same pair isn't
    drilled twice in a row under two anchors."""
    def struggle(k):
        c = state.get(k)
        return (kanji[k]["known"], c["ease"] if c else 3.0)

    groups, used = [], set()
    anchors = (k for k, d in kanji.items()
               if d["lookalikes"] and in_levels(kanji, [k], levels))
    for k in sorted(anchors, key=struggle):
        if k in used:
            continue
        members = [k] + [x for x in kanji[k]["lookalikes"] if x not in used]
        if len(members) < 2:
            continue
        used.update(members)
        groups.append((f"looks alike: {'  '.join(members)}", members))
        if len(groups) == n:
            break
    return groups


def lookalike_session(ctx, state, n_groups, levels=None):
    groups = lookalike_groups(ctx["kanji"], state, n_groups, levels)
    if not groups:
        print(f"no lookalike groups in this vault yet (add a {LOOKALIKES_HEADING} "
              f"section of links to a note)")
        return
    drill_groups(ctx, state, groups, "lookalike groups")


def show_progress(kanji, state, levels=None):
    """Known / in progress (close = next review 14+ days out, hard = missed
    HARD_LAPSES+ times) / not started, per level -- printed after every
    session. Levels outside a --level filter are dimmed."""
    rows = collections.defaultdict(lambda: [0, 0, 0, 0, 0])
    for k, d in kanji.items():
        r = rows[level(d["text"])]
        r[0] += 1
        if d["known"]:
            r[1] += 1
        elif k in state:
            r[2] += 1
            r[3] += state[k].get("ivl", 0) >= 14
            r[4] += is_hard(state[k])
    print(f"\n  {C['b']}{'level':9}{'known':>14}{'learning':>10}{'close':>7}{'hard':>6}{'new':>6}{C['0']}")
    for lvl in P["levels"]:
        t, kn, st, close, hard = rows[lvl]
        if not t:
            continue
        dim = C["dim"] if levels and lvl not in levels else ""
        print(f"  {dim}{P['level_fmt'].format(lvl):9}{kn:>5}/{t:<4}{100 * kn // t:>3}%"
              f"{st:>10}{close:>7}{hard:>6}{t - kn - st:>6}{C['0']}")


def parse_levels(spec):
    """'n5-n4', 'n3', 'n3,n2' (Japanese) or '1-3', '2' (Chinese), in the
    vault's own level names; a range runs in either direction."""
    order = P["levels"]
    out = set()
    for part in spec.lower().replace(" ", "").split(","):
        ends = part.split("-")
        if len(ends) > 2 or any(e not in order for e in ends):
            sys.exit(f"--level {spec!r}: use levels from {', '.join(order)}, "
                     f"e.g. {order[0]}-{order[1]}")
        a, b = sorted(order.index(e) for e in ends) if len(ends) == 2 else [order.index(ends[0])] * 2
        out.update(order[a:b + 1])
    return out


def in_levels(kanji, chars, levels):
    return levels is None or any(level(kanji[k]["text"]) in levels for k in chars)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=".", help="vault directory (default: cwd)")
    ap.add_argument("--new", type=int, default=SESSION_NEW)
    ap.add_argument("--max", type=int, default=SESSION_MAX,
                    help=f"cap total questions this session (default {SESSION_MAX})")
    ap.add_argument("--reserve", type=int, default=SESSION_RESERVE,
                    help=f"slots kept for new cards even with a review backlog "
                    f"(default {SESSION_RESERVE}; --new 0 disables new cards entirely)")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--confusable", type=int, nargs="?", const=5, default=None,
                    metavar="N", help="drill N confusable character clusters "
                    "back-to-back instead of the normal session (default 5)")
    ap.add_argument("--lookalikes", type=int, nargs="?", const=5, default=None,
                    metavar="N", help="drill N groups of characters you mistake for "
                    "each other, from their Lookalikes sections (default 5)")
    ap.add_argument("--hard", type=int, nargs="?", const=8, default=None, metavar="N",
                    help=f"re-study and test the N cards missed most ({HARD_LAPSES}+ misses; "
                    "default 8)")
    ap.add_argument("--level", metavar="RANGE",
                    help="only these levels, e.g. n5-n4 or n3 (Chinese: 1-2)")
    args = ap.parse_args()
    use_vault(args.vault)
    levels = parse_levels(args.level) if args.level else None

    kanji, vocab = build()
    ctx = {
        "kanji": kanji,
        "vocab": vocab,
        "comp_idx": build_comp_index(kanji),
        "comp_meanings": load_component_meanings(),
        "meaning_idx": build_meaning_index(kanji),
    }
    state = load_state()
    ctx["state"] = state

    if args.confusable is not None:
        confusable_session(ctx, state, args.confusable, levels)
    elif args.lookalikes is not None:
        lookalike_session(ctx, state, args.lookalikes, levels)
    elif args.hard is not None:
        hard_session(ctx, state, args.hard, levels)
    elif not review_session(ctx, state, args, levels):
        return
    show_progress(kanji, state, levels)


def review_session(ctx, state, args, levels):
    """The default session. Returns False when nothing was run (--plan, or
    nothing due) so no progress table follows."""
    kanji = ctx["kanji"]
    now = today()

    due_all = [k for k, c in state.items()
              if c.get("due", 0) <= now and k in kanji and not kanji[k]["known"]
              and in_levels(kanji, [k], levels)]
    due_all.sort(key=lambda k: state[k].get("due", 0))  # most overdue first
    reserve = max(0, min(args.reserve, args.new, args.max // 2))
    due = due_all[:args.max - reserve]
    deferred = len(due_all) - len(due)
    fresh = [k for k in frontier(kanji) if k not in state and in_levels(kanji, [k], levels)
             ][:max(reserve, args.max - len(due))][:args.new]

    if args.plan:
        print((f"[{args.level}] " if levels else "") + f"{len(due)} due · {len(fresh)} new queued"
             + (f" · {deferred} due deferred to a later session" if deferred else ""))
        for k in fresh[:25]:
            d = kanji[k]
            why = (f"in {d['half']} known words" if d["half"]
                   else f"unlocks {d['unlocks']}" if d["unlocks"]
                   else level_label(d["text"]))
            print(f"  {k}  {C['dim']}{why:22}{C['0']} {meaning(d['text'])[:46]}")
        return False

    if not due and not fresh:
        print("nothing due. run with --new to pull more.")
        return False
    random.shuffle(due)
    # new cards spread through the session rather than tacked on the end,
    # so quitting early doesn't silently drop the reserved ones
    queue = due[:]
    for j, k in enumerate(fresh):
        queue.insert(round((j + 1) * len(due) / (len(fresh) + 1)) + j, k)

    graded, tagged = run_queue(ctx, state, queue)

    save_state(state)
    remaining = sum(1 for k, d in kanji.items()
                    if not d["known"] and in_levels(kanji, [k], levels))
    print(f"\n{C['dim']}{'─' * 52}{C['0']}")
    print(f"{graded} graded · {tagged} newly tagged known · "
          f"{remaining} {(args.level + ' ') if levels else ''}{P['noun']} left unknown"
          + (f" · {deferred} due held back, raise --max to clear the backlog"
             if deferred else ""))
    return True


def run_queue(ctx, state, queue):
    """The review loop: unseen cards get a study card first and are tested a
    few cards later; a miss comes back once more in the same session."""
    kanji = ctx["kanji"]
    graded = tagged = 0
    requeued, studied = set(), set()
    i = 0
    while i < len(queue):
        k = queue[i]
        i += 1
        if k not in state and k not in studied:
            if not study(k, kanji[k], ctx, i, len(queue)):
                break
            studied.add(k)
            queue.insert(min(len(queue), i + random.randint(3, 6)), k)
            continue
        result = ask(k, kanji[k], ctx, i, len(queue))
        if result is None:
            break
        card = schedule(state.get(k, {}), result)
        state[k] = card
        graded += 1
        if result == 1 and k not in requeued:
            # a miss re-enters the same session a few cards later instead of
            # just waiting for tomorrow's run -- that's what makes it stick
            requeued.add(k)
            pos = min(len(queue), i + random.randint(3, 6))
            queue.insert(pos, k)
            print(f"  {C['dim']}" + (f"back in {pos - i} cards" if pos > i else "back next")
                  + f"{C['0']}")
        elif card["ivl"] >= MATURE:
            mark_known(kanji[k])
            tagged += 1
            print(f"  {C['g']}matured → tagged known{C['0']}")
        else:
            print(f"  {C['dim']}next in {card['ivl']}d{C['0']}")

    return graded, tagged


if __name__ == "__main__":
    main()
