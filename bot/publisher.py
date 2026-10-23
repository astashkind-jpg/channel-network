"""Автоутверждение по дедлайну и публикация постов, время которых наступило."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .core import Context, deadline_passed
from .tg import TelegramError
from .util import esc, now

log = logging.getLogger(__name__)

EXPIRE_AFTER = timedelta(hours=12)  # ручной пост, не утверждённый за 12 ч после своего времени, сгорает


def auto_approve(ctx: Context, queue: dict) -> int:
    if not deadline_passed(queue):
        return 0
    n = 0
    for p in queue["posts"]:
        if p["status"] == "pending" and p.get("auto", True):
            p["status"] = "approved"
            p["approved_by"] = "auto"
            n += 1
    return n


def publish_post(ctx: Context, post: dict) -> dict:
    ch = ctx.channel(post["channel"])
    if not ch or not ch.chat_id:
        raise TelegramError(f"канал {post['channel']} не настроен")
    if post["kind"] == "photo":
        return ctx.bot.send_photo(ch.chat_id, post["media"], post["text"])
    if post["kind"] == "photo_text":
        ctx.bot.send_photo(ch.chat_id, post["media"])
        return ctx.bot.send_text(ch.chat_id, post["text"], preview=False)
    if post["kind"] == "video":
        return ctx.bot.send_video(ch.chat_id, post["media"], post["text"])
    return ctx.bot.send_text(ch.chat_id, post["text"], preview=False)


def publish_due(ctx: Context, queue: dict) -> list[str]:
    """Публикует утверждённые посты, время которых пришло. Возвращает ошибки."""
    errors = []
    paused = set(ctx.state.meta()["paused"])
    current = now()
    for p in sorted(queue["posts"], key=lambda x: x["time"]):
        when = datetime.fromisoformat(p["time"])
        if when > current:
            continue
        if p["status"] == "pending" and current - when > EXPIRE_AFTER:
            p["status"] = "expired"
            continue
        if p["status"] != "approved" or "all" in paused or p["channel"] in paused:
            continue
        try:
            msg = publish_post(ctx, p)
            p["status"] = "published"
            p["published_at"] = current.isoformat()
            p["message_id"] = msg.get("message_id") if isinstance(msg, dict) else None
            log.info("опубликован %s", p["id"])
        except TelegramError as e:
            p["status"] = "failed"
            p["error"] = str(e)[:300]
            errors.append(f"{p['id']}: {e}")
        ctx.state.save_queue(queue)  # сохраняем после каждого поста — без дублей при сбое
    return errors


def tick(ctx: Context) -> None:
    from .approvals import process_updates

    process_updates(ctx)
    today = now().date()
    all_errors = []
    for q in ctx.state.active_queues(today):
        if auto_approve(ctx, q):
            ctx.state.save_queue(q)
        all_errors += publish_due(ctx, q)
        ctx.state.save_queue(q)
    if all_errors and ctx.secrets.admin_chat_id:
        text = "⚠️ <b>Не опубликовалось:</b>\n" + "\n".join(f"• {esc(e[:200])}" for e in all_errors[:10])
        try:
            ctx.bot.send_text(ctx.secrets.admin_chat_id, text)
        except TelegramError as e:
            log.error("не удалось сообщить об ошибках: %s", e)
    ctx.state.cleanup(today)
