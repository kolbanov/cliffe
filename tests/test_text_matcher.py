from __future__ import annotations

import json
import unittest
from pathlib import Path

from app.text_matcher import TextMatcher


BASE_DIR = Path(__file__).resolve().parent.parent


def load_json(path: str) -> object:
    with (BASE_DIR / path).open("r", encoding="utf-8") as file:
        return json.load(file)


class TextMatcherTestCase(unittest.TestCase):
    words_path = ""

    def setUp(self) -> None:
        words = load_json(self.words_path)
        letter_mapping = load_json("data/letter_mapping.json")
        self.assertIsInstance(words, list)
        self.assertIsInstance(letter_mapping, dict)
        self.matcher = TextMatcher(words, letter_mapping)

    def assert_hits(self, text: str) -> None:
        self.assertIsNotNone(self.matcher.find(text), text)

    def assert_misses(self, text: str) -> None:
        self.assertIsNone(self.matcher.find(text), text)


class TextMatcherPoliticsTest(TextMatcherTestCase):
    words_path = "data/stop_words_politics.json"

    def test_related_hohol_forms_are_detected(self) -> None:
        for text in [
            "Хохляндия",
            "хохляндии",
            "хохляндский",
            "хохляндского",
            "Хохляндец",
            "хохляндца",
            "хохлянка",
            "хохлянок",
            "хохландия",
            "хохландского",
            "хохландец",
            "хохланок",
            "хохляцкий",
            "хохлятина",
            "хохлярой",
            "хохлёнок",
            "хохленками",
            "хохлостан",
            "хохлостанца",
            "хохлостанские",
        ]:
            with self.subTest(text=text):
                self.assert_hits(text)


class TextMatcherAsuTest(TextMatcherTestCase):
    words_path = "data/asu_words.json"

    def test_requested_asu_forms_are_detected(self) -> None:
        for text in ["негр", "нига", "нигер", "пидр", "пидарас", "нигретос", "негретос", "негретоска"]:
            with self.subTest(text=text):
                self.assert_hits(text)

    def test_related_asu_forms_are_detected(self) -> None:
        for text in ["негритос", "негритоска", "нигритос", "нигритоска", "нигретоска"]:
            with self.subTest(text=text):
                self.assert_hits(text)

    def test_common_inflected_forms_are_detected(self) -> None:
        for text in [
            "негра",
            "негром",
            "нигеров",
            "негретосом",
            "нигретосов",
            "негретоски",
            "нигретосок",
            "нигритосками",
            "пидру",
            "пидарасами",
            "пидорасов",
        ]:
            with self.subTest(text=text):
                self.assert_hits(text)

    def test_enok_forms_are_detected(self) -> None:
        for text in ["негритенок", "негритёнок", "негритенка", "негритёнку", "негритенками"]:
            with self.subTest(text=text):
                self.assert_hits(text)

    def test_obfuscated_forms_are_detected(self) -> None:
        for text in ["н.е.г.р", "нннииигггааа", "н-е-г-р-е-т-о-с", "п-и-д-р", "п1д@р@с"]:
            with self.subTest(text=text):
                self.assert_hits(text)

    def test_word_boundaries_reduce_false_positives(self) -> None:
        for text in ["книга", "анегреческий", "нигерия"]:
            with self.subTest(text=text):
                self.assert_misses(text)


if __name__ == "__main__":
    unittest.main()
