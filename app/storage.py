from __future__ import annotations

import json
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

STOP_WORDS_PATH = DATA_DIR / "stop_words_politics.json"
ASU_WORDS_PATH = DATA_DIR / "asu_words.json"
LETTER_MAPPING_PATH = DATA_DIR / "letter_mapping.json"
SETTINGS_PATH = DATA_DIR / "settings.json"
WARNINGS_PATH = DATA_DIR / "warnings.json"
KNOWN_CHATS_PATH = DATA_DIR / "known_chats.json"
PHRASES_PATH = DATA_DIR / "phrases.json"

DEFAULT_SETTINGS = {
    "defaults": {
        "warn_limit": 5,
        "mute_days": 1,
    },
    "chats": {},
}

DEFAULT_PHRASES = {
    "warn": [
        "тебя зацепило хаешкой",
        "ты подгорел в молике",
        "ты неудачно спрыгнул с девятки в плент",
        "в тебя дали хит через стену",
        "ты забыл чекнуть тёмку",
        "тебя прострелили через смок",
        "ты поймал флеш от тиммейта",
        "ты пикнул без инфы и словил минус мораль",
    ],
    "mute": [
        "ты не пропрыгнул мид на дасте, тебя убил авик",
        "ты фулблайнд сгорел в молике",
        "тебе дали одну в голову с дигла",
        "ты форсанул без армора и сразу отлетел",
        "ты пикнул мид без флешки и ушёл в спектры",
        "ты забыл диффузы и проиграл клатч",
    ],
    "asu": [
        "АСУ!!!",
    ],
    "mute_error": [
        "авик промазал: я не смог выдать мут. проверь мои права админа или статус цели",
    ],
}


class JsonStorage:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._ensure_files()

    def _ensure_files(self) -> None:
        defaults: dict[Path, Any] = {
            STOP_WORDS_PATH: [],
            ASU_WORDS_PATH: [],
            LETTER_MAPPING_PATH: {},
            SETTINGS_PATH: DEFAULT_SETTINGS,
            WARNINGS_PATH: {},
            KNOWN_CHATS_PATH: {},
            PHRASES_PATH: DEFAULT_PHRASES,
        }
        for path, default in defaults.items():
            if not path.exists():
                self._write_json(path, default)

        # Мягкая миграция для старых сборок: если phrases.json уже есть, но в нём нет новых категорий,
        # добавляем их, не затирая пользовательские фразы.
        phrase_data = self._read_json(PHRASES_PATH, deepcopy(DEFAULT_PHRASES))
        changed = False
        if not isinstance(phrase_data, dict):
            phrase_data = deepcopy(DEFAULT_PHRASES)
            changed = True
        for key, value in DEFAULT_PHRASES.items():
            if key not in phrase_data or not isinstance(phrase_data[key], list):
                phrase_data[key] = value
                changed = True
        if changed:
            self._write_json(PHRASES_PATH, phrase_data)

    def _read_json(self, path: Path, fallback: Any) -> Any:
        with self._lock:
            try:
                with path.open("r", encoding="utf-8") as file:
                    return json.load(file)
            except (FileNotFoundError, json.JSONDecodeError):
                self._write_json(path, fallback)
                return fallback

    def _write_json(self, path: Path, data: Any) -> None:
        with self._lock:
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            with tmp_path.open("w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=2)
                file.write("\n")
            tmp_path.replace(path)

    @staticmethod
    def _normalize_words(words: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for word in words:
            value = str(word).strip().lower()
            if value and value not in seen:
                result.append(value)
                seen.add(value)
        return result

    @staticmethod
    def _normalize_phrases(phrases: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for phrase in phrases:
            value = str(phrase).strip()
            if value and value not in seen:
                result.append(value)
                seen.add(value)
        return result

    def get_letter_mapping(self) -> dict[str, str]:
        data = self._read_json(LETTER_MAPPING_PATH, {})
        return {str(key): str(value) for key, value in data.items()}

    def get_stop_words(self) -> list[str]:
        data = self._read_json(STOP_WORDS_PATH, [])
        return self._normalize_words(data)

    def get_asu_words(self) -> list[str]:
        data = self._read_json(ASU_WORDS_PATH, [])
        return self._normalize_words(data)

    def add_words(self, kind: str, words: list[str]) -> list[str]:
        path = STOP_WORDS_PATH if kind == "stop" else ASU_WORDS_PATH
        current = self._normalize_words(self._read_json(path, []))
        current_set = set(current)
        added: list[str] = []
        for word in self._normalize_words(words):
            if word not in current_set:
                current.append(word)
                current_set.add(word)
                added.append(word)
        self._write_json(path, current)
        return added

    def remove_words(self, kind: str, words: list[str]) -> list[str]:
        path = STOP_WORDS_PATH if kind == "stop" else ASU_WORDS_PATH
        current = self._normalize_words(self._read_json(path, []))
        to_remove = set(self._normalize_words(words))
        removed = [word for word in current if word in to_remove]
        next_words = [word for word in current if word not in to_remove]
        self._write_json(path, next_words)
        return removed

    def get_phrases(self, category: str) -> list[str]:
        data = self._read_json(PHRASES_PATH, deepcopy(DEFAULT_PHRASES))
        values = data.get(category, DEFAULT_PHRASES.get(category, []))
        if not isinstance(values, list):
            values = DEFAULT_PHRASES.get(category, [])
        normalized = self._normalize_phrases(values)
        if not normalized:
            normalized = DEFAULT_PHRASES.get(category, [])
        return normalized

    def get_random_phrase(self, category: str) -> str:
        # random импортируем локально, чтобы storage оставался простым при тестах.
        import random

        phrases = self.get_phrases(category)
        return random.choice(phrases)

    def add_phrases(self, category: str, phrases: list[str]) -> list[str]:
        data = self._read_json(PHRASES_PATH, deepcopy(DEFAULT_PHRASES))
        current = self._normalize_phrases(data.get(category, DEFAULT_PHRASES.get(category, [])))
        current_set = set(current)
        added: list[str] = []
        for phrase in self._normalize_phrases(phrases):
            if phrase not in current_set:
                current.append(phrase)
                current_set.add(phrase)
                added.append(phrase)
        data[category] = current
        self._write_json(PHRASES_PATH, data)
        return added

    def replace_phrases(self, category: str, phrases: list[str]) -> list[str]:
        data = self._read_json(PHRASES_PATH, deepcopy(DEFAULT_PHRASES))
        normalized = self._normalize_phrases(phrases)
        if not normalized:
            normalized = DEFAULT_PHRASES.get(category, [])
        data[category] = normalized
        self._write_json(PHRASES_PATH, data)
        return normalized

    def remove_phrase_by_index(self, category: str, index: int) -> str | None:
        data = self._read_json(PHRASES_PATH, deepcopy(DEFAULT_PHRASES))
        current = self._normalize_phrases(data.get(category, DEFAULT_PHRASES.get(category, [])))
        if index < 0 or index >= len(current):
            return None
        removed = current.pop(index)
        if not current:
            current = DEFAULT_PHRASES.get(category, [])
        data[category] = current
        self._write_json(PHRASES_PATH, data)
        return removed

    def reset_phrases(self, category: str) -> list[str]:
        data = self._read_json(PHRASES_PATH, deepcopy(DEFAULT_PHRASES))
        data[category] = DEFAULT_PHRASES.get(category, [])
        self._write_json(PHRASES_PATH, data)
        return data[category]

    def get_chat_settings(self, chat_id: int) -> dict[str, int]:
        data = self._read_json(SETTINGS_PATH, DEFAULT_SETTINGS)
        defaults = data.get("defaults", DEFAULT_SETTINGS["defaults"])
        chat_settings = data.get("chats", {}).get(str(chat_id), {})
        return {
            "warn_limit": int(chat_settings.get("warn_limit", defaults.get("warn_limit", 5))),
            "mute_days": int(chat_settings.get("mute_days", defaults.get("mute_days", 1))),
        }

    def set_chat_setting(self, chat_id: int, key: str, value: int) -> dict[str, int]:
        data = self._read_json(SETTINGS_PATH, DEFAULT_SETTINGS)
        data.setdefault("defaults", DEFAULT_SETTINGS["defaults"])
        data.setdefault("chats", {})
        chat_settings = data["chats"].setdefault(str(chat_id), {})
        chat_settings[key] = int(value)
        self._write_json(SETTINGS_PATH, data)
        return self.get_chat_settings(chat_id)

    def remember_chat(self, chat_id: int, title: str | None) -> None:
        if not title:
            title = str(chat_id)
        data = self._read_json(KNOWN_CHATS_PATH, {})
        data[str(chat_id)] = title
        self._write_json(KNOWN_CHATS_PATH, data)

    def get_known_chats(self) -> dict[int, str]:
        data = self._read_json(KNOWN_CHATS_PATH, {})
        result: dict[int, str] = {}
        for raw_chat_id, title in data.items():
            try:
                result[int(raw_chat_id)] = str(title)
            except ValueError:
                continue
        return result

    def remember_user(self, chat_id: int, user_id: int, full_name: str, username: str | None) -> None:
        data = self._read_json(WARNINGS_PATH, {})
        chat_data = data.setdefault(str(chat_id), {})
        user_data = chat_data.setdefault(str(user_id), {})
        user_data["full_name"] = full_name
        user_data["username"] = username
        user_data["last_seen"] = datetime.now(timezone.utc).isoformat()
        user_data.setdefault("count", 0)
        self._write_json(WARNINGS_PATH, data)

    def get_warning_count(self, chat_id: int, user_id: int) -> int:
        data = self._read_json(WARNINGS_PATH, {})
        return int(data.get(str(chat_id), {}).get(str(user_id), {}).get("count", 0))

    def set_warning_count(self, chat_id: int, user_id: int, count: int) -> None:
        data = self._read_json(WARNINGS_PATH, {})
        chat_data = data.setdefault(str(chat_id), {})
        user_data = chat_data.setdefault(str(user_id), {})
        user_data["count"] = max(0, int(count))
        user_data["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._write_json(WARNINGS_PATH, data)

    def clear_warning_count(self, chat_id: int, user_id: int) -> None:
        self.set_warning_count(chat_id, user_id, 0)

    def decrement_warning_count(self, chat_id: int, user_id: int) -> int:
        current = self.get_warning_count(chat_id, user_id)
        next_count = max(0, current - 1)
        self.set_warning_count(chat_id, user_id, next_count)
        return next_count

    def get_warnings_for_chat(self, chat_id: int) -> list[dict[str, Any]]:
        data = self._read_json(WARNINGS_PATH, {})
        chat_data = data.get(str(chat_id), {})
        result: list[dict[str, Any]] = []
        for raw_user_id, user_data in chat_data.items():
            count = int(user_data.get("count", 0))
            if count <= 0:
                continue
            try:
                user_id = int(raw_user_id)
            except ValueError:
                continue
            result.append(
                {
                    "user_id": user_id,
                    "count": count,
                    "full_name": str(user_data.get("full_name") or user_id),
                    "username": user_data.get("username"),
                }
            )
        result.sort(key=lambda item: item["count"], reverse=True)
        return result
