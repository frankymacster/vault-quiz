import collections, json

VOICE = str.maketrans("がぎぐげござじずぜぞだぢづでどばびぶべぼぱぴぷぺぽ",
                      "かきくけこさしすせそたちつてとはひふへほはひふへほ")
# full-form parts that are usually meaning parts but do act as the sound in common kanji (問 聞)
PHONETIC_OK = {"門"}
SEMANTIC = set("氵扌言訁忄心糸木金釒口日月火灬土女亻人彳辶宀艹⺮竹疒衤礻示貝刂力門雨頁足𧾷⻖阝犭禾米車馬魚鳥石目耳手衣飠食虫隹山王玉囗广尸冫歹殳攵欠戈弓巾皿穴立舟角酉革")


def hira(s):
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s.replace("-", ""))


def build(kanji, pool, db):
    """Sound families: a part plus the kanji built on it whose on'yomi matches
    the part's own (or, for a part with no reading of its own, at least three
    members sharing one). Voiced variants count as the same sound (こう/ごう)."""
    on = lambda c: [hira(r) for r in (db.get(c, {}).get("readings_on") or [])]
    key = lambda r: r.translate(VOICE)
    by_comp = collections.defaultdict(list)
    for k in pool:
        for c in kanji[k]["comps"]:
            if c != k:
                by_comp[c].append(k)
    fams = {}
    for c, ks in by_comp.items():
        # a meaning part (氵 扌 艹 口...) doesn't give the sound: several water kanji sharing
        # a reading is a coincidence, not a sound family
        if c in SEMANTIC and c not in PHONETIC_OK:
            continue
        own = on(c)
        cands = own or sorted({r for k in ks for r in on(k)})
        best = None
        for r in cands:
            m = [k for k in ks if any(key(x) == key(r) for x in on(k))]
            if len(m) >= (2 if own else 3) and (best is None or len(m) > len(best[1])):
                best = (r, m)
        if best:
            r, m = best
            fams[c] = {"r": r, "k": sorted(m),
                       "a": {k: next(x for x in on(k) if key(x) == key(r)) for k in m}}
    roles = {}
    for k in pool:
        comps = kanji[k]["comps"]
        snd = next((c for c in comps if c in fams and k in fams[c]["k"]), None)
        if snd is None:
            snd = next((c for c in comps if c != k and (c not in SEMANTIC or c in PHONETIC_OK)
                        and set(map(key, on(k))) & set(map(key, on(c)))), None)
        r = {}
        for c in comps:
            if c == snd:
                r[c] = "s"
            elif snd is not None or c in SEMANTIC:
                r[c] = "m"
        roles[k] = r
    return fams, roles, on
