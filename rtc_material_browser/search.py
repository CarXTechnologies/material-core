"""Search Material Core by names, tags and source paths."""
import re
import unicodedata

QUERY = re.compile(r'(?:(tag|тег):)?(?:"([^"]+)"|([^\s"]+))', re.IGNORECASE)
RUSSIAN_ENDINGS = ("ический", "ическая", "ическое", "ические", "иями", "ями", "ами", "ыми", "ими",
                   "ого", "ему", "ому", "его", "ная", "ный", "ные", "ное", "ая", "яя",
                   "ое", "ее", "ые", "ие", "ый", "ий", "ой", "ую", "юю", "ых", "их", "ов", "ам",
                   "ах", "ка", "ки", "а", "я", "ы", "и", "у", "ю", "е", "ь")


def normalize(value):
    value = unicodedata.normalize("NFC", value).casefold().replace("ё", "е")
    return " ".join(re.findall(r"[^\W_]+", value))


def parse(query):
    result = []
    for field, phrase, word in QUERY.findall(query):
        value = normalize(phrase or word)
        if value:
            if phrase or field:
                result.append((bool(field), value, bool(phrase) or " " in value))
            else:
                result.extend((False, token, False) for token in value.split())
    return result


def stem(word):
    if len(word) >= 5 and re.fullmatch("[а-я]+", word):
        for ending in RUSSIAN_ENDINGS:
            if len(word) - len(ending) >= 4 and word.endswith(ending):
                return word[:-len(ending)]
    return word


def one_edit(first, second):
    if abs(len(first) - len(second)) > 1:
        return False
    position = next((index for index, (a, b) in enumerate(zip(first, second)) if a != b),
                    min(len(first), len(second)))
    a, b = first[position:], second[position:]
    if len(first) == len(second):
        return a[1:] == b[1:] or (len(a) > 1 and a[:2] == b[:2][::-1] and a[2:] == b[2:])
    return a == b[1:] if len(first) < len(second) else a[1:] == b


def rank(terms, fields):
    score = 0
    for tags_only, term, phrase in terms:
        candidates = fields[1:2] if tags_only else fields
        best = 0
        root = stem(term)
        for text, tokens, roots, priority in candidates:
            if phrase:
                if f" {term} " in f" {text} ":
                    best = max(best, priority + 6)
            elif term in tokens:
                best = max(best, priority + 6)
            elif (len(root) >= 4 and root in roots) or any(token.startswith(term) for token in tokens):
                best = max(best, priority + 2)
            elif not tags_only and any(term in token for token in tokens):
                best = max(best, priority)
            elif not tags_only and len(term) >= 5 and term.isalpha() and any(
                    token.isalpha() and one_edit(term, token) for token in tokens):
                best = max(best, 1)
        if best == 0:
            return 0
        score += best
    return score


def fields(name, tags, other):
    priority = ((name, 14), (" ".join(tags), 8), (other, 3))
    result = []
    for value, weight in priority:
        text = normalize(value)
        tokens = frozenset(text.split())
        result.append((text, tokens, frozenset(stem(token) for token in tokens), weight))
    return result
