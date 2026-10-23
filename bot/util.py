"""Вспомогательные функции: время, окна публикаций, HTML, разбор JSON от ИИ."""
from __future__ import annotations

import html
import json
import random
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Moscow")

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]


def now() -> datetime:
    return datetime.now(TZ)


def human_date(d: date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]}, {WEEKDAYS[d.weekday()]}"


def short_date(d: date) -> str:
    return f"{d.day:02d}.{d.month:02d} ({WEEKDAYS_SHORT[d.weekday()]})"


def parse_window(window: str) -> tuple[time, time]:
    start, end = window.split("-")
    h1, m1 = map(int, start.strip().split(":"))
    h2, m2 = map(int, end.strip().split(":"))
    return time(h1, m1), time(h2, m2)


def random_time_in_window(day: date, window: str, rng: random.Random) -> datetime:
    start, end = parse_window(window)
    a = datetime.combine(day, start, TZ)
    b = datetime.combine(day, end, TZ)
    if b <= a:
        b = a + timedelta(minutes=1)
    seconds = int((b - a).total_seconds())
    # время кратно 1 минуте, без секунд
    minute = rng.randrange(0, max(seconds // 60, 1))
    return a + timedelta(minutes=minute)


def esc(text: str) -> str:
    """Экранирование для parse_mode=HTML."""
    return html.escape(text or "", quote=False)


def extract_json(text: str) -> dict:
    """Достаёт первый JSON-объект из ответа модели (модели любят обрамлять JSON текстом)."""
    if not text:
        raise ValueError("пустой ответ модели")
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    depth = 0
    start = None
    in_str = False
    escape = False
    for i, ch in enumerate(cleaned):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                return json.loads(cleaned[start:i + 1])
    raise ValueError(f"в ответе нет JSON: {text[:200]}")


def weighted_choice(items: list, weights: list[float], rng: random.Random):
    return rng.choices(items, weights=weights, k=1)[0]
