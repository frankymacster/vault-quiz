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
    python3 quiz.py --vault <vault> --confusable [N]   # drill N lookalike clusters

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


def get_mnemonic(text):
    m = re.search(re.escape(MNEMONIC_HEADING) + r"\n(.*?)(?=\n### |\n---\s*$)",
                  text, re.S)
    return m.group(1).strip() if m else None


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


def schedule(card, grade):
    """SM-2, lightly trimmed. grade: 1 again, 2 hard, 3 good, 4 easy."""
    ease = card.get("ease", 2.5)
    reps = card.get("reps", 0)
    ivl = card.get("ivl", 0)
    if grade == 1:
        reps, ivl, ease = 0, 0, max(1.3, ease - 0.2)
    else:
        if reps == 0:
            ivl = 1
        elif reps == 1:
            ivl = 6
        else:
            ivl = max(1, round(ivl * (ease if grade >= 3 else 1.2)))
        if grade == 2:
            ease = max(1.3, ease - 0.15)
        elif grade == 4:
            ivl = round(ivl * 1.3)
            ease += 0.15
        reps += 1
    card.update(ease=round(ease, 2), reps=reps, ivl=ivl, due=today() + ivl)
    return card


# ---------------------------------------------------------------- session

STOPWORDS = {"a", "an", "the", "to", "of", "or", "and", "for", "with",
            "this", "that", "one", "some", "etc"}


def significant_words(s):
    return [w for w in re.findall(r"[a-zA-Z']+", s.lower())
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
        for w in significant_words(meaning(d["text"])):
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
    return [k2 for k2, _ in hits.most_common(n)]


class _Quit(Exception):
    pass


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


def show_similar(k, d, kanji, comp_idx):
    groups = similar_by_component(k, d, kanji, comp_idx)
    if not groups:
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
    ("m", "mnemonic"), ("q", "quit"),
])
RETRY_MENU = menu("try again", [
    ("c", "components"), ("v", "vocab"), ("s", "similar"),
    ("m", "mnemonic"), ("g", "give up"),
])


STRAY_KEYS = {"c", "v", "s", "g", "q", "k", "h"}   # every letter that means
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


def do_mnemonic(k, d):
    if d["mnemonic"]:
        print(f"  {C['y']}your mnemonic:{C['0']} {d['mnemonic']}")
        edit = _read(f"{C['dim']}  enter to keep it, or type a replacement{C['0']}  ")
        if edit:
            save_mnemonic_if_real(d, edit, "updated")
    else:
        note = _read(f"{C['dim']}  write a mnemonic (blank to skip){C['0']}  ")
        if note:
            save_mnemonic_if_real(d, note, "saved")


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
    is "good"; only an explicit give-up is "again"."""
    vocab, kanji, comp_idx = ctx["vocab"], ctx["kanji"], ctx["comp_idx"]
    comp_meanings, meaning_idx = ctx["comp_meanings"], ctx["meaning_idx"]
    print(f"\n{C['dim']}{'─' * 52}{C['0']}")
    print(f"{C['dim']}{n}/{total}   {level_label(d['text'])}{C['0']}")
    print(f"\n      {C['b']}{C['c']}{k}{C['0']}\n")
    try:
        guess = _read(FIRST_MENU)
        cold = True
        answer = meaning(d["text"])
        misses = 0
        while True:
            low = guess.lower()
            if low == "c":
                cold = False
                show_components(d, comp_meanings)
            elif low == "v":
                cold = False
                show_vocab_hint(d, vocab)
            elif low == "s":
                cold = False
                show_similar(k, d, kanji, comp_idx)
            elif low == "m":
                cold = False
                do_mnemonic(k, d)
            elif not guess or low == "g":
                print(f"  {C['r']}✗ {k} means:{C['0']} {answer}")
                grade = 1
                if d["unlocks"]:
                    print(f"  {C['dim']}unlocks {d['unlocks']} more words{C['0']}")
                if d["mnemonic"]:
                    print(f"  {C['y']}your mnemonic:{C['0']} {d['mnemonic']}")
                    _read(f"{C['dim']}  enter for next{C['0']}  ")
                else:
                    note = _read(f"{C['dim']}  write a mnemonic for next "
                                f"time (blank to skip){C['0']}  ")
                    if note:
                        save_mnemonic_if_real(d, note, "saved")
                return grade
            elif is_match(guess, answer):
                print(f"  {C['g']}✓ {answer}{C['0']}")
                grade = 4 if cold else 3
                break
            else:
                cold = False
                misses += 1
                print(f'  {C["r"]}✗ not quite ("{guess}"){C["0"]}')
                mixups = find_mixup(guess, k, kanji, meaning_idx)
                if mixups:
                    print(f"  {C['y']}that's the meaning of:{C['0']}")
                    for k2 in mixups:
                        print(f"    {k2}  ({meaning(kanji[k2]['text'])})")
                    print(f"  {C['dim']}maybe that's what you're thinking of{C['0']}")
                # repeated blind guessing trains the wrong association -- force
                # a real clue in rather than let it loop unaided. your own
                # mnemonic (if any) is the cheapest, most tailored one, so it
                # surfaces before the generic vocab/component/similar aids
                if misses == 1 and d["mnemonic"]:
                    print(f"  {C['y']}your mnemonic:{C['0']} {d['mnemonic']}")
                elif misses == 2:
                    print(f"  {C['y']}two misses -- here's a hint:{C['0']}")
                    (show_vocab_hint(d, vocab) if d["vocab"]
                    else show_components(d, comp_meanings))
                elif misses >= 3:
                    print(f"  {C['y']}still stuck -- visual family:{C['0']}")
                    show_similar(k, d, kanji, comp_idx)
            guess = _read(RETRY_MENU)
    except _Quit:
        return None

    if d["unlocks"]:
        print(f"  {C['dim']}unlocks {d['unlocks']} more words{C['0']}")
    return grade


def confusable_session(ctx, state, n_clusters):
    """Drill lookalike kanji as a group, back-to-back, instead of scattered
    across a normal session -- discrimination is the point, so the contrast
    has to be felt in the moment, not encountered accidentally weeks apart."""
    kanji, comp_idx = ctx["kanji"], ctx["comp_idx"]
    clusters = rank_clusters(build_clusters(kanji, comp_idx), kanji)[:n_clusters]
    if not clusters:
        print("no confusable clusters need drilling right now")
        return

    graded = tagged = 0
    for anchor, members, unknown in clusters:
        print(f"\n{C['b']}{C['c']}confusable group -- shares {anchor}{C['0']}"
             f"  {C['dim']}({unknown} unknown of {len(members)}){C['0']}")
        order = members[:]
        random.shuffle(order)
        for i, k in enumerate(order, 1):
            result = ask(k, kanji[k], ctx, i, len(order))
            if result is None:
                save_state(state)
                print(f"\n{graded} graded across clusters so far")
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

        print(f"\n  {C['y']}side by side:{C['0']}")
        for k in members:
            tag = (f"{C['g']}known{C['0']}" if kanji[k]["known"]
                  else f"{C['dim']}unknown{C['0']}")
            print(f"    {k}  {meaning(kanji[k]['text']):30}  {tag}")

    save_state(state)
    print(f"\n{C['dim']}{'─' * 52}{C['0']}")
    print(f"{graded} graded across {len(clusters)} clusters · "
          f"{tagged} newly tagged known")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=".", help="vault directory (default: cwd)")
    ap.add_argument("--new", type=int, default=SESSION_NEW)
    ap.add_argument("--max", type=int, default=SESSION_MAX,
                    help=f"cap total questions this session (default {SESSION_MAX})")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--confusable", type=int, nargs="?", const=5, default=None,
                    metavar="N", help="drill N confusable character clusters "
                    "back-to-back instead of the normal session (default 5)")
    args = ap.parse_args()
    use_vault(args.vault)

    kanji, vocab = build()
    ctx = {
        "kanji": kanji,
        "vocab": vocab,
        "comp_idx": build_comp_index(kanji),
        "comp_meanings": load_component_meanings(),
        "meaning_idx": build_meaning_index(kanji),
    }
    state = load_state()
    now = today()

    if args.confusable is not None:
        confusable_session(ctx, state, args.confusable)
        return

    due_all = [k for k, c in state.items()
              if c.get("due", 0) <= now and k in kanji and not kanji[k]["known"]]
    due_all.sort(key=lambda k: state[k].get("due", 0))  # most overdue first
    due = due_all[:args.max]
    deferred = len(due_all) - len(due)
    fresh = [k for k in frontier(kanji) if k not in state][:max(0, args.max - len(due))][:args.new]

    if args.plan:
        print(f"{len(due)} due · {len(fresh)} new queued"
             + (f" · {deferred} due deferred (raise --max to see them)" if deferred else ""))
        for k in fresh[:25]:
            d = kanji[k]
            why = (f"in {d['half']} known words" if d["half"]
                   else f"unlocks {d['unlocks']}" if d["unlocks"]
                   else level_label(d["text"]))
            print(f"  {k}  {C['dim']}{why:22}{C['0']} {meaning(d['text'])[:46]}")
        return

    if not due and not fresh:
        print("nothing due. run with --new to pull more.")
        return
    random.shuffle(due)
    queue = due + fresh

    graded = tagged = 0
    requeued = set()
    i = 0
    while i < len(queue):
        k = queue[i]
        i += 1
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
            print(f"  {C['dim']}back in {pos - i} cards{C['0']}")
        elif card["ivl"] >= MATURE:
            mark_known(kanji[k])
            tagged += 1
            print(f"  {C['g']}matured → tagged known{C['0']}")
        else:
            print(f"  {C['dim']}next in {card['ivl']}d{C['0']}")

    save_state(state)
    remaining = sum(1 for d in kanji.values() if not d["known"])
    print(f"\n{C['dim']}{'─' * 52}{C['0']}")
    print(f"{graded} graded · {tagged} newly tagged known · "
          f"{remaining} {P['noun']} left unknown"
          + (f" · {deferred} due held back, raise --max to clear the backlog"
             if deferred else ""))


if __name__ == "__main__":
    main()
