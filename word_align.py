"""Split a word's kana reading across its characters: 頂く いただく ->
[頂 いただ kun] [く く kana]. Each kanji takes one of its listed on/kun readings,
allowing rendaku (voicing of the first kana) and a small っ for a final つくちき;
a kanji whose reading isn't listed (or a whole irregular word like 今日) takes
whatever is left, marked '?'."""
import re

_VOICED = dict(zip("かきくけこさしすせそたちつてとはひふへほ", "がぎぐげござじずぜぞだぢづでどばびぶべぼ"))
_HALF = dict(zip("はひふへほ", "ぱぴぷぺぽ"))


def hira(s):
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


def is_kana(c):
    return "ぁ" <= c <= "ゖ" or "ァ" <= c <= "ヺ" or c in "ー・"


def variants(r):
    out = {r}
    if r[0] in _VOICED:
        out.add(_VOICED[r[0]] + r[1:])
    if r[0] in _HALF:
        out.add(_HALF[r[0]] + r[1:])
    for v in list(out):
        if len(v) > 1 and v[-1] in "つくちき":
            out.add(v[:-1] + "っ")
    return out


_IROW = dict(zip("うくぐすつぬぶむる", "いきぎしちにびみり"))


def readings_of(entry):
    """On readings, and kun readings with the okurigana a compound drops or
    bends: し.める -> し, しめ; き.る -> き, きり; ま.つ -> ま, まち."""
    on = [hira(r) for r in entry.get("readings_on", [])]
    kun = []
    for r in entry.get("readings_kun", []):
        stem, _, okuri = hira(r).strip("-").partition(".")
        kun.append(stem)
        if okuri:
            kun.append(stem + okuri)
            kun.append(stem + okuri[:-1])
            if okuri[-1] in _IROW:
                kun.append(stem + okuri[:-1] + _IROW[okuri[-1]])
    return [r for r in dict.fromkeys(on) if r], [r for r in dict.fromkeys(kun) if r]


def align(word, reading, kdb):
    reading = hira(reading)
    units = []
    for c in word:
        if is_kana(c) and units and units[-1][1] == "kana":
            units[-1][0] += c
        else:
            units.append([c, "kana" if is_kana(c) else "kanji"])
    best = None

    def go(i, pos, acc, wild, prev):
        nonlocal best
        if best is not None and best[1] <= wild:
            return
        if i == len(units):
            if pos == len(reading):
                best = (list(acc), wild)
            return
        text, kind = units[i]
        rest = reading[pos:]
        if kind == "kana":
            t = hira(text)
            if rest.startswith(t):
                go(i + 1, pos + len(t), acc + [[text, t, ""]], wild, prev)
            return
        k = prev if text == "々" else text
        on, kun = readings_of(kdb.get(k, {}))
        tried = set()
        for kind_, rs in (("o", on), ("k", kun)):
            for r in rs:
                for v in sorted(variants(r), key=len, reverse=True):
                    if (v, kind_) not in tried and rest.startswith(v):
                        tried.add((v, kind_))
                        go(i + 1, pos + len(v), acc + [[text, v, kind_]], wild, k)
        for n in range(1, min(6, len(rest)) + 1):
            go(i + 1, pos + n, acc + [[text, rest[:n], "?"]], wild + 1, k)

    go(0, 0, [], 0, None)
    if best is None or best[1] > 1:
        return [[word, reading, "?"]]
    return best[0]
