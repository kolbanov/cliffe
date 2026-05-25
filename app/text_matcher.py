from __future__ import annotations

import re
from dataclasses import dataclass

SEPARATOR = r"(?:[^\w\s]|_)*"
BOUNDARY_CHARS = r"0-9A-Za-zА-Яа-яЁё"
LEFT_BOUNDARY = rf"(?<![{BOUNDARY_CHARS}])"
RIGHT_BOUNDARY = rf"(?![{BOUNDARY_CHARS}])"
MAX_CHAR_REPEAT = 4

EXACT_ONLY_WORDS = {
    "сво",
    "рф",
    "всу",
    "зсу",
    "лнр",
    "днр",
    "оон",
    "нато",
}

CONSONANT_ENDINGS = [
    "",
    "а",
    "у",
    "ом",
    "ем",
    "ым",
    "им",
    "е",
    "ы",
    "и",
    "ов",
    "ев",
    "ам",
    "ям",
    "ами",
    "ями",
    "ах",
    "ях",
    "ский",
    "ская",
    "ское",
    "ские",
    "ского",
    "скому",
    "ским",
    "ском",
    "ских",
    "скими",
]

SKY_ENDINGS = [
    "ий",
    "ого",
    "ому",
    "им",
    "ом",
    "ая",
    "ую",
    "ое",
    "ие",
    "их",
    "ими",
]

ADJECTIVE_ENDINGS = [
    "ый",
    "ий",
    "ой",
    "ого",
    "ему",
    "ому",
    "ым",
    "им",
    "ом",
    "ая",
    "ую",
    "ое",
    "ые",
    "ие",
    "ых",
    "их",
    "ыми",
    "ими",
]

IA_ENDINGS = [
    "я",
    "и",
    "ю",
    "ей",
    "е",
    "ями",
    "ях",
]

A_ENDINGS = [
    "а",
    "ы",
    "е",
    "у",
    "ой",
    "ою",
    "ами",
    "ах",
]

EC_ENDINGS = [
    "ец",
    "ца",
    "цу",
    "цем",
    "це",
    "цы",
    "цев",
    "цам",
    "цами",
    "цах",
]

ENGLISH_ENDINGS = ["", "s", "es"]

CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)
LATIN_RE = re.compile(r"[a-z]", re.IGNORECASE)


def unique_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


def candidate_forms(word: str) -> list[str]:
    word = word.strip().lower()

    if not word:
        return []

    if word in EXACT_ONLY_WORDS:
        return [word]

    # English words: nigger/niggers, nigga/niggas, faggot/faggots.
    if LATIN_RE.search(word) and not CYRILLIC_RE.search(word):
        forms = [word + ending for ending in ENGLISH_ENDINGS]
        if word.endswith("s"):
            forms.append(word[:-1])
        return unique_keep_order(forms)

    # зеленский -> зеленского, зеленскому, зеленские...
    if word.endswith("ский"):
        stem = word[:-2]  # зеленск
        return unique_keep_order([stem + ending for ending in SKY_ENDINGS])

    # черномазый / черножопый -> черномазого, черномазые...
    if word.endswith(("ый", "ий", "ой")):
        stem = word[:-2]
        return unique_keep_order([stem + ending for ending in ADJECTIVE_ENDINGS])

    # мобилизация, оккупация, санкция, репрессия...
    if word.endswith("ия"):
        stem = word[:-1]
        return unique_keep_order([stem + ending for ending in IA_ENDINGS])

    # бандеровец -> бандеровцы, бандеровцев...
    if word.endswith("ец"):
        stem = word[:-2]
        return unique_keep_order([stem + ending for ending in EC_ENDINGS])

    # дума, пропаганда, либераха, рашка...
    if word.endswith("а"):
        stem = word[:-1]
        return unique_keep_order([stem + ending for ending in A_ENDINGS])

    # слова во множественном числе, которые лучше не склонять механически: выборы.
    if word.endswith(("ы", "и")):
        return [word]

    return unique_keep_order([word + ending for ending in CONSONANT_ENDINGS])


def build_fragment(text: str, letter_mapping: dict[str, str]) -> str:
    parts: list[str] = []

    for char in text.lower():
        base = letter_mapping.get(char, re.escape(char))

        if char in {"ь", "ъ"}:
            parts.append(base)
        else:
            parts.append(f"(?:{base}){{1,{MAX_CHAR_REPEAT}}}")

    return SEPARATOR.join(parts)


def build_word_regex(word: str, letter_mapping: dict[str, str]) -> re.Pattern[str] | None:
    forms = candidate_forms(word)
    if not forms:
        return None

    variants = [build_fragment(form, letter_mapping) for form in forms]
    pattern = LEFT_BOUNDARY + r"(?:" + "|".join(variants) + r")" + RIGHT_BOUNDARY
    return re.compile(pattern, re.IGNORECASE | re.UNICODE)


@dataclass(slots=True)
class MatchResult:
    word: str
    matched_text: str


class TextMatcher:
    def __init__(self, words: list[str], letter_mapping: dict[str, str]) -> None:
        self.rebuild(words, letter_mapping)

    def rebuild(self, words: list[str], letter_mapping: dict[str, str]) -> None:
        self._patterns: list[tuple[str, re.Pattern[str]]] = []
        for word in unique_keep_order([item.strip().lower() for item in words if item.strip()]):
            pattern = build_word_regex(word, letter_mapping)
            if pattern is not None:
                self._patterns.append((word, pattern))

    def find(self, text: str | None) -> MatchResult | None:
        if not text:
            return None

        for word, pattern in self._patterns:
            match = pattern.search(text)
            if match:
                return MatchResult(word=word, matched_text=match.group(0))

        return None
