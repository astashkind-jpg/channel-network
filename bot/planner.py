"""Вечерний план на следующий день и сводка для согласования."""
from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime, timedelta

from .core import Context, add_promo, catalog_posts, plan_channel
from .tg import TelegramError, keyboard
from .util import esc, human_date, now

log = logging.getLogger(__name__)

KIND_NAMES = {"photo": "фото", "video": "видео", "text": "текст"}


def plan_day(ctx: Context, day: date, force: bool = False) -> dict:
    existing = ctx.state.load_queue(day)
    if existing and not force:
        log.info("план на %s уже есть — пропускаю (используйте --force)", day)
        return existing

    posts: list[dict] = []
    errors: list[str] = []
    for ch in ctx.ready_channels():
        log.info("планирую %s", ch.key)
        try:
            posts += plan_channel(ctx, ch, day, errors)
        except Exception as e:
            log.exception("канал %s", ch.key)
            errors.append(f"{ch.title}: {str(e)[:200]}")
        record_members(ctx, ch)
    if day.weekday() == ctx.network.catalog_weekday:
        posts += catalog_posts(ctx, day)
    add_promo(ctx, [p for p in posts if p["rubric"] != "Каталог сети"])
    posts.sort(key=lambda p: p["time"])

    queue = {
        "date": day.isoformat(),
        "created": now().isoformat(),
        "deadline": (now() + timedelta(hours=ctx.network.review_hours)).isoformat(),
        "posts": posts,
        "errors": errors,
        "digest_message_id": None,
    }
    ctx.state.save_queue(queue)
    send_digest(ctx, queue)
    ctx.state.save_queue(queue)
    return queue


def record_members(ctx: Context, ch) -> None:
    try:
        count = ctx.bot.member_count(ch.chat_id)
    except TelegramError as e:
        log.warning("подписчики %s: %s", ch.key, e)
        return
    h = ctx.state.history(ch.key)
    today = now().date().isoformat()
    h["members"] = [m for m in h["members"] if m["date"] != today] + [{"date": today, "count": count}]
    ctx.state.save_history(ch.key, h)


def digest_text(ctx: Context, queue: dict) -> str:
    day = date.fromisoformat(queue["date"])
    lines = [f"📋 <b>План на {human_date(day)}</b>", ""]
    by_channel: dict[str, list[dict]] = {}
    for p in queue["posts"]:
        by_channel.setdefault(p["channel"], []).append(p)
    for ch in ctx.channels:
        items = by_channel.get(ch.key)
        if not items:
            continue
        kinds = Counter(KIND_NAMES.get(p["kind"], p["kind"]) for p in items)
        detail = ", ".join(f"{n} {k}" for k, n in kinds.items())
        lines.append(f"{ch.emoji} {esc(ch.title)} — {len(items)} ({detail})")
    if not queue["posts"]:
        lines.append("Постов нет: ни один канал не готов (enabled + chat_id) или всё упало.")
    manual = Counter(ctx.channel(p["channel"]).title for p in queue["posts"]
                     if not p["auto"] and p["status"] == "pending" and ctx.channel(p["channel"]))
    deadline = datetime.fromisoformat(queue["deadline"])
    lines += ["", f"⏳ Без ответа посты выйдут автоматически после {deadline:%H:%M}."]
    if manual:
        lst = ", ".join(f"{esc(t)} — {n}" for t, n in manual.items())
        lines.append(f"✋ Выйдут только после вашего ✅: {lst}.")
    if queue.get("errors"):
        lines += ["", "⚠️ <b>Не получилось:</b>"] + [f"• {esc(e)}" for e in queue["errors"][:10]]
    return "\n".join(lines)


def digest_buttons(queue: dict) -> dict:
    d = queue["date"]
    return keyboard([[("✅ Утвердить всё", f"A:{d}"), ("👀 Посмотреть", f"V:{d}")]])


def send_digest(ctx: Context, queue: dict) -> None:
    if not ctx.secrets.admin_chat_id:
        log.warning("ADMIN_CHAT_ID не задан — сводка не отправлена")
        return
    msg = ctx.bot.send_text(ctx.secrets.admin_chat_id, digest_text(ctx, queue), digest_buttons(queue))
    queue["digest_message_id"] = msg["message_id"]
