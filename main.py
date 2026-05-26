from __future__ import annotations

import asyncio
import html
import os
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)
from dotenv import load_dotenv

from app.storage import JsonStorage
from app.text_matcher import MatchResult, TextMatcher

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(ENV_PATH)
load_dotenv()

BOT_TOKEN = os.getenv("TG_BOT_API_KEY")
BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "Europe/Moscow")

# Можно явно прописать группы, которыми бот должен управлять.
# Формат в .env: TG_GROUP_CHAT_IDS=-1001111111111,-1002222222222
GROUP_CHAT_IDS_RAW = os.getenv("TG_GROUP_CHAT_IDS", "")


def parse_configured_chat_ids(raw_value: str) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()

    # Поддерживаем форматы:
    # TG_GROUP_CHAT_IDS=-1001111111111,-1002222222222
    # TG_GROUP_CHAT_IDS="-1001111111111, -1002222222222"
    # TG_GROUP_CHAT_IDS=[-1001111111111, -1002222222222]
    cleaned = raw_value.strip().strip("'\"").replace("[", " ").replace("]", " ")

    for chunk in re.split(r"[,;\s]+", cleaned):
        value = chunk.strip().strip("'\"")
        if not value:
            continue
        try:
            chat_id = int(value)
        except ValueError:
            continue
        if chat_id not in seen:
            result.append(chat_id)
            seen.add(chat_id)
    return result


CONFIGURED_GROUP_CHAT_IDS = parse_configured_chat_ids(GROUP_CHAT_IDS_RAW)

if not BOT_TOKEN:
    raise RuntimeError("Не найден TG_BOT_API_KEY в .env")

try:
    DISPLAY_TZ = ZoneInfo(BOT_TIMEZONE)
except ZoneInfoNotFoundError:
    DISPLAY_TZ = ZoneInfo("UTC")

storage = JsonStorage()
stop_matcher = TextMatcher(storage.get_stop_words(), storage.get_letter_mapping())
asu_matcher = TextMatcher(storage.get_asu_words(), storage.get_letter_mapping())

pending_actions: dict[int, dict[str, str | int]] = {}

PHRASE_CATEGORIES = {
    "warn": "⚠️ Предупреждения",
    "mute": "💀 Муты",
    "asu": "📢 ASU",
    "mute_error": "💥 Ошибка мута",
}

PHRASE_DESCRIPTIONS = {
    "warn": "Фразы используются в предупреждении. В конце бот сам добавит: у тебя осталось ??хп.",
    "mute": "Фразы используются при муте. В конце бот сам добавит: у тебя осталось 0хп. иди тренируйся до ...",
    "asu": "Фразы отправляются ответом на ASU-слово. Можно оставить просто АСУ!!! или добавить несколько вариантов.",
    "mute_error": "Фразы используются, если бот не смог выдать мут. В конце бот сам добавит: у тебя осталось 0хп.",
}


def escape(value: str | int) -> str:
    return html.escape(str(value), quote=True)


def mention_user(user_id: int, full_name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{escape(full_name)}</a>'


def mention_from_user(user: User) -> str:
    return mention_user(user.id, user.full_name)


def calculate_hp(warnings: int, warn_limit: int) -> int:
    if warn_limit <= 0:
        return 0
    return max(0, round((warn_limit - warnings) / warn_limit * 100))


def parse_words(text: str) -> list[str]:
    normalized = text.replace(",", "\n").replace(";", "\n")
    words: list[str] = []
    seen: set[str] = set()
    for line in normalized.splitlines():
        value = line.strip().lower()
        if value and value not in seen:
            words.append(value)
            seen.add(value)
    return words


def parse_command_words(text: str) -> list[str]:
    words: list[str] = []
    seen: set[str] = set()
    for chunk in re.split(r"[,;\s]+", text):
        value = chunk.strip().lower().strip(".,!?()[]{}<>\"'«»“”„`")
        if value and value not in seen:
            words.append(value)
            seen.add(value)
    return words


def parse_phrase_lines(text: str) -> list[str]:
    phrases: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        value = line.strip()
        if value and value not in seen:
            phrases.append(value)
            seen.add(value)
    return phrases


def message_html_text(message: Message) -> str:
    # html_text сохраняет Telegram-форматирование, которое админ поставил в клиенте:
    # жирный, курсив, code, ссылки и т.д. Если форматирования нет, вернётся безопасный текст.
    return (message.html_text or message.text or "").strip()


def phrase_sentence(phrase: str) -> str:
    value = phrase.strip()
    without_tags = re.sub(r"<[^>]+>", "", value).rstrip()
    if without_tags.endswith((".", "!", "?", "…")):
        return value
    return value + "."


async def send_html_message(bot: Bot, chat_id: int, text: str, **kwargs: object) -> None:
    try:
        await bot.send_message(chat_id, text, **kwargs)
    except TelegramBadRequest:
        await bot.send_message(chat_id, escape(text), **kwargs)


def rebuild_matchers() -> None:
    letter_mapping = storage.get_letter_mapping()
    stop_matcher.rebuild(storage.get_stop_words(), letter_mapping)
    asu_matcher.rebuild(storage.get_asu_words(), letter_mapping)


def action_label(action: str) -> str:
    return {
        "add_stop": "добавить политические стоп-слова",
        "del_stop": "удалить политические стоп-слова",
        "add_asu": "добавить ASU-слова",
        "del_asu": "удалить ASU-слова",
        "set_warn_limit": "задать лимит предупреждений",
        "set_mute_days": "задать длительность мута",
        "send_message": "отправить сообщение в выбранную группу",
        "add_phrase": "добавить реплику",
        "replace_phrases": "заменить список реплик",
    }.get(action, action)


def callback(action: str, chat_id: int, *parts: object) -> str:
    values = ["p", str(chat_id), action, *[str(part) for part in parts]]
    return ":".join(values)


def main_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🧨 Полит-стопы", callback_data=callback("stop", chat_id))],
            [InlineKeyboardButton(text="⚠️ АСУ-слова", callback_data=callback("asu", chat_id))],
            [InlineKeyboardButton(text="❤️ Предупреждения", callback_data=callback("warns", chat_id))],
            [InlineKeyboardButton(text="📣 Сообщение в чат", callback_data=callback("send_message", chat_id))],
            [InlineKeyboardButton(text="🎲 Реплики бота", callback_data=callback("phrases", chat_id))],
            [InlineKeyboardButton(text="⚙️ Настройки наказаний", callback_data=callback("settings", chat_id))],
        ]
    )


def words_keyboard(chat_id: int, kind: str) -> InlineKeyboardMarkup:
    add_action = "add_stop" if kind == "stop" else "add_asu"
    del_action = "del_stop" if kind == "stop" else "del_asu"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ добавить", callback_data=callback(add_action, chat_id)),
                InlineKeyboardButton(text="➖ удалить", callback_data=callback(del_action, chat_id)),
            ],
            [InlineKeyboardButton(text="⬅️ назад", callback_data=callback("main", chat_id))],
        ]
    )


def settings_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="-1 пред", callback_data=callback("warn_down", chat_id)),
                InlineKeyboardButton(text="+1 пред", callback_data=callback("warn_up", chat_id)),
            ],
            [
                InlineKeyboardButton(text="-1 день", callback_data=callback("mute_down", chat_id)),
                InlineKeyboardButton(text="+1 день", callback_data=callback("mute_up", chat_id)),
            ],
            [
                InlineKeyboardButton(text="задать преды", callback_data=callback("set_warn_limit", chat_id)),
                InlineKeyboardButton(text="задать дни", callback_data=callback("set_mute_days", chat_id)),
            ],
            [InlineKeyboardButton(text="⬅️ назад", callback_data=callback("main", chat_id))],
        ]
    )


def send_confirm_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ отправить", callback_data=callback("send_confirm", chat_id)),
                InlineKeyboardButton(text="❌ отменить", callback_data=callback("send_cancel", chat_id)),
            ],
            [InlineKeyboardButton(text="⬅️ в панель", callback_data=callback("main", chat_id))],
        ]
    )


def phrases_menu_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=title, callback_data=callback("phrase_cat", chat_id, category))]
            for category, title in PHRASE_CATEGORIES.items()
        ] + [[InlineKeyboardButton(text="⬅️ назад", callback_data=callback("main", chat_id))]]
    )


def phrase_category_keyboard(chat_id: int, category: str, phrases_count: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="➕ добавить", callback_data=callback("phrase_add", chat_id, category))],
        [InlineKeyboardButton(text="🧹 заменить весь список", callback_data=callback("phrase_replace", chat_id, category))],
    ]

    delete_rows: list[list[InlineKeyboardButton]] = []
    for index in range(min(phrases_count, 10)):
        delete_rows.append(
            [InlineKeyboardButton(text=f"🗑 удалить #{index + 1}", callback_data=callback("phrase_del_idx", chat_id, category, index))]
        )
    if delete_rows:
        rows.extend(delete_rows)

    rows.extend(
        [
            [InlineKeyboardButton(text="♻️ вернуть дефолт", callback_data=callback("phrase_reset", chat_id, category))],
            [InlineKeyboardButton(text="⬅️ к репликам", callback_data=callback("phrases", chat_id))],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def warnings_keyboard(chat_id: int, warnings: list[dict], limit: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for item in warnings[:15]:
        user_id = item["user_id"]
        short_name = str(item["full_name"])
        if len(short_name) > 18:
            short_name = short_name[:17] + "…"
        rows.append(
            [
                InlineKeyboardButton(text=f"-1 {short_name}", callback_data=callback("warn_one", chat_id, user_id)),
                InlineKeyboardButton(text=f"снять все", callback_data=callback("warn_all", chat_id, user_id)),
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ назад", callback_data=callback("main", chat_id))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_words_section(title: str, words: list[str]) -> str:
    preview = "\n".join(f"• {escape(word)}" for word in words[:80])
    if len(words) > 80:
        preview += f"\n…и ещё {len(words) - 80}"
    if not preview:
        preview = "пусто, закупаемся на эко"
    return f"<b>{escape(title)}</b>\nВсего: {len(words)}\n\n{preview}"


def format_phrases_menu() -> str:
    return (
        "<b>🎲 Реплики бота</b>\n"
        "Тут можно менять случайные фразы для предупреждений, мутов, ASU и ошибки мута.\n\n"
        "Форматирование из Telegram сохраняется: жирный, курсив, code, ссылки. "
        "Лучше делать форматирование прямо в клиенте Telegram, а не писать HTML руками."
    )


def format_phrase_section(category: str) -> str:
    title = PHRASE_CATEGORIES.get(category, category)
    description = PHRASE_DESCRIPTIONS.get(category, "")
    phrases = storage.get_phrases(category)

    lines = [f"<b>{escape(title)}</b>", escape(description), f"Всего: {len(phrases)}", ""]
    for index, phrase in enumerate(phrases[:30], start=1):
        # phrase уже хранится как безопасный HTML из message.html_text, поэтому не экранируем его полностью.
        lines.append(f"{index}. {phrase}")
    if len(phrases) > 30:
        lines.append(f"…и ещё {len(phrases) - 30}")
    return "\n".join(lines)


def format_settings(chat_id: int) -> str:
    settings = storage.get_chat_settings(chat_id)
    return (
        "<b>⚙️ Настройки наказаний</b>\n"
        f"Критический лимит: {settings['warn_limit']} предупреждений\n"
        f"Мут: {settings['mute_days']} дн.\n\n"
        "Схема простая: набрал лимит — улетел тренироваться." 
    )


def format_warnings(chat_id: int) -> str:
    settings = storage.get_chat_settings(chat_id)
    limit = settings["warn_limit"]
    warnings = storage.get_warnings_for_chat(chat_id)

    if not warnings:
        return "<b>❤️ Предупреждения</b>\nПока все играют аккуратно: у всех 100хп."

    lines = ["<b>❤️ Предупреждения</b>", "От большего количества предов к меньшему:\n"]
    for index, item in enumerate(warnings[:15], start=1):
        hp = calculate_hp(item["count"], limit)
        mention = mention_user(item["user_id"], item["full_name"])
        lines.append(f"{index}. {mention} — {item['count']}/{limit}, {hp}хп")
    if len(warnings) > 15:
        lines.append(f"\nПоказаны первые 15, всего игроков с предами: {len(warnings)}")
    return "\n".join(lines)


async def is_group_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
    except (TelegramBadRequest, TelegramForbiddenError):
        return False
    return member.status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}


def yes_no(value: bool | None) -> str:
    if value is None:
        return "неизвестно"
    return "да" if value else "нет"


async def append_current_chat_diagnostics(lines: list[str], message: Message, bot: Bot) -> None:
    if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return

    lines.extend(
        [
            "",
            "<b>текущая группа</b>",
            f"title: <code>{escape(message.chat.title or message.chat.id)}</code>",
            f"chat_id: <code>{message.chat.id}</code>",
        ]
    )

    try:
        bot_user = await bot.get_me()
        bot_member = await bot.get_chat_member(message.chat.id, bot_user.id)
        bot_status = str(bot_member.status)
        bot_can_restrict = bool(getattr(bot_member, "can_restrict_members", False))
    except (TelegramBadRequest, TelegramForbiddenError):
        bot_status = "не удалось проверить"
        bot_can_restrict = None

    user_is_admin = False
    if message.from_user:
        user_is_admin = await is_group_admin(bot, message.chat.id, message.from_user.id)

    lines.extend(
        [
            f"статус бота: <code>{escape(bot_status)}</code>",
            f"бот может ограничивать участников: <code>{yes_no(bot_can_restrict)}</code>",
            f"ты админ здесь: <code>{yes_no(user_is_admin)}</code>",
        ]
    )

    if bot_can_restrict is False:
        lines.append("дай боту админку с правом ограничивать участников, иначе муты не сработают")


async def delete_safely(message: Message) -> None:
    try:
        await message.delete()
    except (TelegramBadRequest, TelegramForbiddenError):
        pass


async def send_or_edit_panel_message(
    bot: Bot,
    user_id: int,
    chat_id: int,
    text: str | None = None,
    keyboard: InlineKeyboardMarkup | None = None,
) -> None:
    chat_title = storage.get_known_chats().get(chat_id, str(chat_id))
    panel_text = text or f"<b>CS2-модератор</b>\nНастраиваем плент: {escape(chat_title)}"
    await bot.send_message(user_id, panel_text, reply_markup=keyboard or main_keyboard(chat_id))


async def send_panel_to_private(message: Message, bot: Bot, chat_id: int) -> None:
    if not message.from_user:
        return

    if not await is_group_admin(bot, chat_id, message.from_user.id):
        await message.reply("тебе не дали админку, закупка отменяется")
        return

    try:
        await send_or_edit_panel_message(bot, message.from_user.id, chat_id)
        if message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
            await message.reply("панель улетела в личку, чекни радар")
            await delete_safely(message)
    except TelegramForbiddenError:
        await message.reply("сначала напиши мне в личку /start, а потом снова жми /panel")


async def resolve_chat_title(bot: Bot, chat_id: int) -> str:
    try:
        chat = await bot.get_chat(chat_id)
    except (TelegramBadRequest, TelegramForbiddenError):
        return str(chat_id)
    return chat.title or str(chat_id)


async def remember_configured_chats(bot: Bot) -> None:
    for chat_id in CONFIGURED_GROUP_CHAT_IDS:
        title = await resolve_chat_title(bot, chat_id)
        storage.remember_chat(chat_id, title)


async def get_panel_chats_for_user(bot: Bot, user_id: int) -> dict[int, str]:
    # Источники групп:
    # 1. known_chats.json — группы, которые бот увидел через апдейты.
    # 2. TG_GROUP_CHAT_IDS — группы, явно прописанные в .env.
    # Это убирает зависимость от того, пришёл ли боту апдейт из тестовой/основной группы.
    known_chats = storage.get_known_chats()

    for chat_id in CONFIGURED_GROUP_CHAT_IDS:
        if chat_id not in known_chats:
            title = await resolve_chat_title(bot, chat_id)
            storage.remember_chat(chat_id, title)
            known_chats[chat_id] = title

    available: dict[int, str] = {}
    for chat_id, title in known_chats.items():
        if await is_group_admin(bot, chat_id, user_id):
            available[chat_id] = title
    return available


async def remember_bot_chat(event: ChatMemberUpdated) -> None:
    if event.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return

    # Запоминаем группу, когда бота добавили/сделали админом/изменили его статус.
    # Это нужно, чтобы админ мог открывать /panel в личке, не засоряя группу командами.
    storage.remember_chat(event.chat.id, event.chat.title)


async def cmd_start(message: Message) -> None:
    await message.answer(
        "я CS2-модератор: ловлю политоту, считаю хп и кричу АСУ!!!\n"
        "добавь меня в группу админом, дай право ограничивать участников. "
        "группы можно явно прописать в .env через TG_GROUP_CHAT_IDS"
    )


async def cmd_connect(message: Message, bot: Bot) -> None:
    if not message.from_user:
        return

    if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await message.answer("/connect работает в группе, не в соло-лобби")
        return

    storage.remember_chat(message.chat.id, message.chat.title)
    storage.remember_user(
        message.chat.id,
        message.from_user.id,
        message.from_user.full_name,
        message.from_user.username,
    )

    if not await is_group_admin(bot, message.chat.id, message.from_user.id):
        reply = await message.reply("подключать сервер могут только админы")
    else:
        try:
            await send_or_edit_panel_message(bot, message.from_user.id, message.chat.id)
            reply = await message.reply("сервер запомнил, панель улетела в личку")
        except TelegramForbiddenError:
            reply = await message.reply("сервер запомнил. теперь напиши мне в личку /start, потом /panel")

    # Чистим одноразовую команду из группы, чтобы не шуметь в чате.
    await asyncio.sleep(3)
    await delete_safely(message)
    await delete_safely(reply)


async def cmd_chatid(message: Message) -> None:
    if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await message.answer("/chatid работает в группе")
        return
    await message.reply(f"chat_id этого сервера: <code>{message.chat.id}</code>")


async def cmd_testasu(message: Message) -> None:
    if not message.text:
        return

    text = message.text.partition(" ")[2].strip()
    if not text:
        await message.reply("напиши так: <code>/testasu нигретос</code>")
        return

    hit = asu_matcher.find(text)
    if hit:
        await message.reply(
            "ASU-матчер сработал:\n"
            f"словарь: <code>{escape(hit.word)}</code>\n"
            f"фрагмент: <code>{escape(hit.matched_text)}</code>"
        )
        return

    await message.reply("ASU-матчер не сработал на этот текст")


async def add_dictionary_words_from_command(message: Message, bot: Bot, kind: str) -> None:
    if not message.from_user:
        return

    if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await message.reply("эта команда работает в группе")
        return

    if not await is_group_admin(bot, message.chat.id, message.from_user.id):
        await message.reply("добавлять слова могут только админы")
        return

    command_text = message.text or message.caption or ""
    argument_text = command_text.partition(" ")[2].strip()
    source_text = argument_text

    if not source_text and message.reply_to_message:
        source_text = message.reply_to_message.text or message.reply_to_message.caption or ""

    words = parse_command_words(source_text)
    if not words:
        command = "pol" if kind == "stop" else "asu"
        await message.reply(f"напиши так: <code>/{command} слово</code> или ответь командой на сообщение")
        return

    added = storage.add_words(kind, words)
    rebuild_matchers()

    title = "полит-стопы" if kind == "stop" else "ASU-слова"
    if not added:
        await message.reply(f"ничего не изменилось: эти {title} уже были в словаре")
        return

    await message.reply(f"добавлено в {title}:\n" + "\n".join(f"• {escape(word)}" for word in added))


async def cmd_pol(message: Message, bot: Bot) -> None:
    await add_dictionary_words_from_command(message, bot, "stop")


async def cmd_asu(message: Message, bot: Bot) -> None:
    await add_dictionary_words_from_command(message, bot, "asu")


async def cmd_hp(message: Message) -> None:
    if not message.from_user:
        return

    if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await message.answer("/hp работает в группе, не в соло-лобби")
        return

    if message.from_user.is_bot:
        return

    storage.remember_chat(message.chat.id, message.chat.title)
    storage.remember_user(
        message.chat.id,
        message.from_user.id,
        message.from_user.full_name,
        message.from_user.username,
    )

    settings = storage.get_chat_settings(message.chat.id)
    warn_limit = max(1, settings["warn_limit"])
    warnings = storage.get_warning_count(message.chat.id, message.from_user.id)
    warnings = max(0, min(warnings, warn_limit))
    hp = calculate_hp(warnings, warn_limit)

    await message.reply(
        f"{mention_from_user(message.from_user)}, у тебя {hp}хп "
        f"({warnings}/{warn_limit} предупреждений)"
    )


async def cmd_config(message: Message, bot: Bot) -> None:
    if not message.from_user:
        return

    raw = GROUP_CHAT_IDS_RAW or "<пусто>"
    parsed = ", ".join(str(x) for x in CONFIGURED_GROUP_CHAT_IDS) or "<пусто>"
    known = storage.get_known_chats()

    lines = [
        "<b>диагностика конфига</b>",
        f".env path: <code>{escape(ENV_PATH)}</code>",
        f".env найден: <code>{ENV_PATH.exists()}</code>",
        f"cwd: <code>{escape(os.getcwd())}</code>",
        f"TG_GROUP_CHAT_IDS raw: <code>{escape(raw)}</code>",
        f"TG_GROUP_CHAT_IDS parsed: <code>{escape(parsed)}</code>",
        f"known_chats: <code>{len(known)}</code>",
        f"ASU-слов в памяти: <code>{asu_matcher.count()}</code>",
        f"ASU-слова первые 20: <code>{escape(', '.join(asu_matcher.words()[:20]))}</code>",
        f"ASU-фраз: <code>{len(storage.get_phrases('asu'))}</code>",
    ]

    await append_current_chat_diagnostics(lines, message, bot)

    if known:
        lines.append("")
        lines.append("<b>known_chats.json</b>")
        for chat_id, title in known.items():
            lines.append(f"• <code>{chat_id}</code> — {escape(title)}")

    if CONFIGURED_GROUP_CHAT_IDS:
        lines.append("")
        lines.append("<b>проверка групп из .env</b>")
        for chat_id in CONFIGURED_GROUP_CHAT_IDS:
            title = await resolve_chat_title(bot, chat_id)
            try:
                admin = await is_group_admin(bot, chat_id, message.from_user.id)
                admin_text = "админ" if admin else "не админ"
            except Exception:
                admin_text = "не удалось проверить"
            lines.append(f"• <code>{chat_id}</code> — {escape(title)} — {admin_text}")

    await message.answer("\n".join(lines))


async def cmd_panel(message: Message, bot: Bot) -> None:
    if not message.from_user:
        return

    if message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        storage.remember_chat(message.chat.id, message.chat.title)
        if await is_group_admin(bot, message.chat.id, message.from_user.id):
            try:
                await send_or_edit_panel_message(bot, message.from_user.id, message.chat.id)
                await message.reply("панель отправил в личку, но лучше открывай её сразу там: /panel")
            except TelegramForbiddenError:
                await message.reply("напиши мне в личку /start, потом там же /panel")
        else:
            await message.reply("панель только для админов, без закупа не пускаю")
        return

    available_chats = await get_panel_chats_for_user(bot, message.from_user.id)
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=title, callback_data=callback("main", chat_id))]
        for chat_id, title in available_chats.items()
    ]

    if not rows:
        if CONFIGURED_GROUP_CHAT_IDS:
            await message.answer(
                "я вижу группы из TG_GROUP_CHAT_IDS, но не нашёл среди них чатов, где ты админ.\n\n"
                "проверь, что:\n"
                "1. chat_id указаны верно, обычно они начинаются с -100\n"
                "2. бот добавлен в обе группы\n"
                "3. ты админ в этих группах"
            )
        else:
            await message.answer(
                "я пока не вижу групп, где ты админ.\n\n"
                "если ты уже прописал TG_GROUP_CHAT_IDS, значит бот сейчас не видит эту переменную. "
                "проверь через /config.\n\n"
                "правильный формат в .env:\n"
                "TG_GROUP_CHAT_IDS=-1001111111111,-1002222222222\n\n"
                "или сделай один раз так:\n"
                "1. в группе напиши /connect\n"
                "2. я запомню группу и попробую удалить команду\n"
                "3. вернись сюда и открой /panel"
            )
        return

    if len(available_chats) == 1:
        chat_id, chat_title = next(iter(available_chats.items()))
        await message.answer(
            f"<b>CS2-модератор</b>\nНастраиваем плент: {escape(chat_title)}",
            reply_markup=main_keyboard(chat_id),
        )
        return

    await message.answer(
        "выбери сервер, где будем мутить токсиков:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def cmd_bite(message: Message, bot: Bot) -> None:
    if not message.from_user:
        return

    if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await message.reply("/bite работает в группе, не в соло-лобби")
        return

    storage.remember_chat(message.chat.id, message.chat.title)

    if not await is_group_admin(bot, message.chat.id, message.from_user.id):
        await message.reply("кусать могут только админы, у тебя закуп без дефузов")
        return

    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply("кого кусать?")
        return

    target = message.reply_to_message.from_user
    if target.is_bot:
        await message.reply("ботов кусать бесполезно, у них хитбоксы сломаны")
        return

    await issue_warning(
        bot=bot,
        chat_id=message.chat.id,
        target=target,
        source_message=message.reply_to_message,
        manual=True,
    )


async def mute_user(bot: Bot, chat_id: int, user_id: int, mute_days: int) -> datetime:
    until_date = datetime.now(timezone.utc) + timedelta(days=mute_days)
    permissions = ChatPermissions(
        can_send_messages=False,
        can_send_audios=False,
        can_send_documents=False,
        can_send_photos=False,
        can_send_videos=False,
        can_send_video_notes=False,
        can_send_voice_notes=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
        can_change_info=False,
        can_invite_users=False,
        can_pin_messages=False,
        can_manage_topics=False,
    )
    await bot.restrict_chat_member(
        chat_id=chat_id,
        user_id=user_id,
        permissions=permissions,
        until_date=until_date,
    )
    return until_date


def format_until(until_date: datetime) -> str:
    local_dt = until_date.astimezone(DISPLAY_TZ)
    return local_dt.strftime("%d.%m.%Y %H:%M")


async def issue_warning(
    bot: Bot,
    chat_id: int,
    target: User,
    source_message: Message,
    manual: bool = False,
    trigger: MatchResult | None = None,
) -> None:
    settings = storage.get_chat_settings(chat_id)
    warn_limit = max(1, settings["warn_limit"])
    mute_days = max(1, settings["mute_days"])

    storage.remember_user(chat_id, target.id, target.full_name, target.username)

    current_count = storage.get_warning_count(chat_id, target.id)
    new_count = current_count + 1
    storage.set_warning_count(chat_id, target.id, new_count)

    if new_count < warn_limit:
        hp = calculate_hp(new_count, warn_limit)
        phrase = phrase_sentence(storage.get_random_phrase("warn"))
        await send_html_message(
            bot,
            chat_id,
            f"{mention_from_user(target)}, {phrase} у тебя осталось {hp}хп",
            reply_to_message_id=source_message.message_id,
        )
        return

    hp = 0
    phrase = phrase_sentence(storage.get_random_phrase("mute"))
    try:
        until_date = await mute_user(bot, chat_id, target.id, mute_days)
        storage.clear_warning_count(chat_id, target.id)
        await send_html_message(
            bot,
            chat_id,
            f"{mention_from_user(target)}, {phrase} у тебя осталось {hp}хп. "
            f"иди тренируйся до {format_until(until_date)}",
            reply_to_message_id=source_message.message_id,
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        phrase = phrase_sentence(storage.get_random_phrase("mute_error"))
        await send_html_message(
            bot,
            chat_id,
            f"{mention_from_user(target)}, {phrase} у тебя осталось {hp}хп",
            reply_to_message_id=source_message.message_id,
        )


async def handle_private_pending(message: Message) -> None:
    if not message.from_user or not message.text:
        return

    pending = pending_actions.get(message.from_user.id)
    if not pending:
        return

    action = str(pending["action"])
    chat_id = int(pending["chat_id"])

    if action in {"add_stop", "del_stop", "add_asu", "del_asu"}:
        words = parse_words(message.text)
        if not words:
            await message.answer("пустая закупка, кинь слово или список слов")
            return

        kind = "stop" if action.endswith("stop") else "asu"
        if action.startswith("add"):
            changed = storage.add_words(kind, words)
            verb = "добавлено"
        else:
            changed = storage.remove_words(kind, words)
            verb = "удалено"

        rebuild_matchers()
        pending_actions.pop(message.from_user.id, None)
        if not changed:
            await message.answer("ничего не изменилось: либо уже было, либо такого не нашли")
            return
        await message.answer(f"{verb}:\n" + "\n".join(f"• {escape(word)}" for word in changed))
        return

    if action == "send_message":
        text = message.text.strip()
        if not text:
            await message.answer("пустой бай не отправляем, кинь текст сообщения")
            return

        if len(text) > 4096:
            await message.answer(f"текст длиннее лимита Telegram: {len(text)}/4096 символов")
            return

        chat_title = storage.get_known_chats().get(chat_id, str(chat_id))
        pending_actions[message.from_user.id] = {"action": "confirm_send", "chat_id": chat_id, "text": text}
        await message.answer(
            f"<b>Проверь перед отправкой на сервер {escape(chat_title)}:</b>\n\n"
            f"{escape(text)}\n\n"
            "Отправляем?",
            reply_markup=send_confirm_keyboard(chat_id),
        )
        return

    if action in {"add_phrase", "replace_phrases"}:
        category = str(pending.get("category", ""))
        if category not in PHRASE_CATEGORIES:
            pending_actions.pop(message.from_user.id, None)
            await message.answer("категория реплик потерялась, радар шумит")
            return

        html_text = message_html_text(message)
        phrases = parse_phrase_lines(html_text)
        if not phrases:
            await message.answer("пустую реплику не сохраняем, кинь текст")
            return

        if action == "add_phrase":
            changed = storage.add_phrases(category, phrases)
            pending_actions.pop(message.from_user.id, None)
            if not changed:
                await message.answer("такая реплика уже есть, повторка в смок")
                return
            await message.answer("добавлено:\n" + "\n".join(f"• {phrase}" for phrase in changed))
            return

        changed = storage.replace_phrases(category, phrases)
        pending_actions.pop(message.from_user.id, None)
        await message.answer("список реплик заменён:\n" + "\n".join(f"• {phrase}" for phrase in changed[:20]))
        return

    if action in {"set_warn_limit", "set_mute_days"}:
        try:
            value = int(message.text.strip())
        except ValueError:
            await message.answer("нужна цифра, не раскидка через смок")
            return

        if value < 1:
            await message.answer("меньше 1 нельзя, это уже чит-код")
            return

        key = "warn_limit" if action == "set_warn_limit" else "mute_days"
        storage.set_chat_setting(chat_id, key, value)
        pending_actions.pop(message.from_user.id, None)
        await message.answer("настройка принята, закуп обновлён")
        return


async def handle_group_message(message: Message, bot: Bot) -> None:
    if not message.from_user or message.from_user.is_bot:
        return

    if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return

    storage.remember_chat(message.chat.id, message.chat.title)
    storage.remember_user(message.chat.id, message.from_user.id, message.from_user.full_name, message.from_user.username)

    text = message.text or message.caption or ""
    if not text:
        return

    if text.lstrip().startswith("/"):
        return

    politics_hit = stop_matcher.find(text)
    if politics_hit:
        # Приоритет стоп-слова выше ASU: если стоп-слово найдено, ASU-реакцию не делаем.
        await issue_warning(
            bot=bot,
            chat_id=message.chat.id,
            target=message.from_user,
            source_message=message,
            manual=False,
            trigger=politics_hit,
        )
        return

    asu_hit = asu_matcher.find(text)
    if asu_hit:
        try:
            await message.reply(storage.get_random_phrase("asu"))
        except TelegramBadRequest:
            await message.reply(escape(storage.get_random_phrase("asu")))


async def callback_panel(query: CallbackQuery, bot: Bot) -> None:
    if not query.data or not query.from_user:
        return

    parts = query.data.split(":")
    if len(parts) < 3 or parts[0] != "p":
        return

    try:
        chat_id = int(parts[1])
    except ValueError:
        await query.answer("сломанный айди сервера", show_alert=True)
        return

    action = parts[2]

    if not await is_group_admin(bot, chat_id, query.from_user.id):
        await query.answer("без админки сюда нельзя", show_alert=True)
        return

    if action == "main":
        chat_title = storage.get_known_chats().get(chat_id, str(chat_id))
        await query.message.edit_text(
            f"<b>CS2-модератор</b>\nНастраиваем плент: {escape(chat_title)}",
            reply_markup=main_keyboard(chat_id),
        )
        await query.answer()
        return

    if action == "stop":
        await query.message.edit_text(
            format_words_section("🧨 Политические стоп-слова", storage.get_stop_words()),
            reply_markup=words_keyboard(chat_id, "stop"),
        )
        await query.answer()
        return

    if action == "asu":
        await query.message.edit_text(
            format_words_section("ASU-слова", storage.get_asu_words()),
            reply_markup=words_keyboard(chat_id, "asu"),
        )
        await query.answer()
        return

    if action == "phrases":
        await query.message.edit_text(format_phrases_menu(), reply_markup=phrases_menu_keyboard(chat_id))
        await query.answer()
        return

    if action == "phrase_cat" and len(parts) >= 4:
        category = parts[3]
        if category not in PHRASE_CATEGORIES:
            await query.answer("непонятная категория реплик", show_alert=True)
            return
        phrases = storage.get_phrases(category)
        await query.message.edit_text(
            format_phrase_section(category),
            reply_markup=phrase_category_keyboard(chat_id, category, len(phrases)),
        )
        await query.answer()
        return

    if action in {"phrase_add", "phrase_replace"} and len(parts) >= 4:
        category = parts[3]
        if category not in PHRASE_CATEGORIES:
            await query.answer("непонятная категория реплик", show_alert=True)
            return
        pending_action = "add_phrase" if action == "phrase_add" else "replace_phrases"
        pending_actions[query.from_user.id] = {"action": pending_action, "chat_id": chat_id, "category": category}
        mode_text = "добавить новую реплику" if action == "phrase_add" else "заменить весь список реплик"
        await query.message.answer(
            f"Ок, надо {mode_text} для раздела {escape(PHRASE_CATEGORIES[category])}.\n"
            "Кинь текст в следующем сообщении. Telegram-форматирование сохраню.\n"
            "Если отправишь несколько строк, каждая строка станет отдельной репликой."
        )
        await query.answer()
        return

    if action == "phrase_del_idx" and len(parts) >= 5:
        category = parts[3]
        if category not in PHRASE_CATEGORIES:
            await query.answer("непонятная категория реплик", show_alert=True)
            return
        try:
            index = int(parts[4])
        except ValueError:
            await query.answer("сломанный номер реплики", show_alert=True)
            return
        removed = storage.remove_phrase_by_index(category, index)
        if removed is None:
            await query.answer("реплика уже ушла в спектры", show_alert=True)
            return
        phrases = storage.get_phrases(category)
        await query.message.edit_text(
            format_phrase_section(category),
            reply_markup=phrase_category_keyboard(chat_id, category, len(phrases)),
        )
        await query.answer("реплика удалена")
        return

    if action == "phrase_reset" and len(parts) >= 4:
        category = parts[3]
        if category not in PHRASE_CATEGORIES:
            await query.answer("непонятная категория реплик", show_alert=True)
            return
        storage.reset_phrases(category)
        phrases = storage.get_phrases(category)
        await query.message.edit_text(
            format_phrase_section(category),
            reply_markup=phrase_category_keyboard(chat_id, category, len(phrases)),
        )
        await query.answer("вернул дефолт")
        return

    if action == "send_message":
        chat_title = storage.get_known_chats().get(chat_id, str(chat_id))
        pending_actions[query.from_user.id] = {"action": action, "chat_id": chat_id}
        await query.message.answer(
            f"Кинь текст, который я отправлю в группу {escape(chat_title)}.\n"
            "После текста покажу превью и попрошу подтвердить отправку."
        )
        await query.answer()
        return

    if action in {"send_confirm", "send_cancel"}:
        pending = pending_actions.get(query.from_user.id)

        if action == "send_cancel":
            pending_actions.pop(query.from_user.id, None)
            await query.message.edit_text("Отменил отправку. Сообщение осталось на базе, в чат не улетело.")
            await query.answer("отменено")
            return

        if not pending or pending.get("action") != "confirm_send" or int(pending.get("chat_id", 0)) != chat_id:
            await query.message.edit_text("черновик не найден или уже отыгран")
            await query.answer("черновик не найден", show_alert=True)
            return

        text = str(pending.get("text", "")).strip()
        if not text:
            pending_actions.pop(query.from_user.id, None)
            await query.message.edit_text("текст пустой, нечего отправлять")
            await query.answer("пусто", show_alert=True)
            return

        try:
            await bot.send_message(chat_id, text, parse_mode=None)
        except (TelegramBadRequest, TelegramForbiddenError):
            await query.message.edit_text("не смог отправить в группу, проверь что я там есть и могу писать")
            await query.answer("не отправилось", show_alert=True)
            return

        pending_actions.pop(query.from_user.id, None)
        chat_title = storage.get_known_chats().get(chat_id, str(chat_id))
        await query.message.edit_text(f"Сообщение улетело в {escape(chat_title)}. Раунд начался.")
        await query.answer("отправлено")
        return

    if action in {"add_stop", "del_stop", "add_asu", "del_asu", "set_warn_limit", "set_mute_days"}:
        pending_actions[query.from_user.id] = {"action": action, "chat_id": chat_id}
        await query.message.answer(
            f"Ок, сейчас надо {escape(action_label(action))}.\n"
            "Кинь одним сообщением слово или список слов с новой строки."
        )
        await query.answer()
        return

    if action == "warns":
        settings = storage.get_chat_settings(chat_id)
        warnings = storage.get_warnings_for_chat(chat_id)
        await query.message.edit_text(
            format_warnings(chat_id),
            reply_markup=warnings_keyboard(chat_id, warnings, settings["warn_limit"]),
        )
        await query.answer()
        return

    if action == "settings":
        await query.message.edit_text(
            format_settings(chat_id),
            reply_markup=settings_keyboard(chat_id),
        )
        await query.answer()
        return

    if action in {"warn_up", "warn_down", "mute_up", "mute_down"}:
        settings = storage.get_chat_settings(chat_id)
        if action == "warn_up":
            storage.set_chat_setting(chat_id, "warn_limit", settings["warn_limit"] + 1)
        elif action == "warn_down":
            storage.set_chat_setting(chat_id, "warn_limit", max(1, settings["warn_limit"] - 1))
        elif action == "mute_up":
            storage.set_chat_setting(chat_id, "mute_days", settings["mute_days"] + 1)
        elif action == "mute_down":
            storage.set_chat_setting(chat_id, "mute_days", max(1, settings["mute_days"] - 1))

        await query.message.edit_text(format_settings(chat_id), reply_markup=settings_keyboard(chat_id))
        await query.answer("закуп обновлён")
        return

    if action in {"warn_one", "warn_all"} and len(parts) >= 4:
        try:
            user_id = int(parts[3])
        except ValueError:
            await query.answer("сломанный айди игрока", show_alert=True)
            return

        if action == "warn_one":
            storage.decrement_warning_count(chat_id, user_id)
            await query.answer("снял один пред")
        else:
            storage.clear_warning_count(chat_id, user_id)
            await query.answer("снял все преды")

        settings = storage.get_chat_settings(chat_id)
        warnings = storage.get_warnings_for_chat(chat_id)
        await query.message.edit_text(
            format_warnings(chat_id),
            reply_markup=warnings_keyboard(chat_id, warnings, settings["warn_limit"]),
        )
        return

    await query.answer("непонятная кнопка, радар шумит", show_alert=True)


async def main() -> None:
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    await remember_configured_chats(bot)
    dp = Dispatcher()

    dp.my_chat_member.register(remember_bot_chat)
    dp.message.register(cmd_start, Command("start"))
    dp.message.register(cmd_panel, Command("panel"))
    dp.message.register(cmd_connect, Command("connect"))
    dp.message.register(cmd_chatid, Command("chatid"))
    dp.message.register(cmd_testasu, Command("testasu"))
    dp.message.register(cmd_pol, Command("pol"))
    dp.message.register(cmd_asu, Command("asu"))
    dp.message.register(cmd_config, Command("config"))
    dp.message.register(cmd_hp, Command("hp"))
    dp.message.register(cmd_bite, Command("bite"))
    dp.message.register(handle_private_pending, F.chat.type == ChatType.PRIVATE, F.text)
    # Ловим любые сообщения в группе, чтобы запоминать чат даже по стикеру/фото.
    # Если у бота включён privacy mode и он не админ, Telegram всё равно не пришлёт обычные сообщения —
    # для первичной привязки тогда используется /connect.
    dp.message.register(handle_group_message)
    dp.callback_query.register(callback_panel, F.data.startswith("p:"))

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
