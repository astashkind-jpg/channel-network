"""Еженедельный отчёт: подписчики, прирост, публикации, просмотры."""
from __future__ import annotations

import logging
from datetime import date, timedelta

from . import reposter
from .core import Context
from .planner import record_members
from .util import esc, now

log = logging.getLogger(__name__)


def members_delta(history: dict, days: int = 7) -> tuple[int | None, int | None]:
    members = history.get("members", [])
    if not members:
        return None, None
    current = members[-1]["count"]
    cutoff = (now().date() - timedelta(days=days)).isoformat()
    older = [m for m in members if m["date"] <= cutoff]
    base = older[-1]["count"] if older else members[0]["count"]
    return current, current - base


def weekly_stats(ctx: Context, days: int = 7) -> dict[str, dict]:
    today = now().date()
    stats: dict[str, dict] = {}
    for i in range(days):
        q = ctx.state.load_queue(today - timedelta(days=i))
        if not q:
            continue
        for p in q["posts"]:
            s = stats.setdefault(p["channel"], {"published": 0, "rejected": 0, "failed": 0})
            if p["status"] in s:
                s[p["status"]] += 1
    return stats


def send_report(ctx: Context) -> str:
    stats = weekly_stats(ctx)
    lines = [f"📊 <b>Отчёт за неделю</b> (по {now():%d.%m})", ""]
    total_members = 0
    total_growth = 0
    for ch in ctx.ready_channels():
        record_members(ctx, ch)
        count, delta = members_delta(ctx.state.history(ch.key))
        s = stats.get(ch.key, {})
        views = reposter.channel_views(ch.chat_id, 7, ctx.secrets)
        parts = [f"{ch.emoji} <b>{esc(ch.title)}</b>"]
        if count is not None:
            total_members += count
            total_growth += delta or 0
            sign = "+" if (delta or 0) >= 0 else ""
            parts.append(f"   👥 {count:,}".replace(",", " ") + f" ({sign}{delta})")
        parts.append(f"   📤 опубликовано {s.get('published', 0)}, ❌ отклонено {s.get('rejected', 0)}"
                     + (f", ⚠️ ошибок {s['failed']}" if s.get("failed") else ""))
        if views:
            parts.append(f"   👁 в среднем {views['avg']:,}".replace(",", " ")
                         + f", лучший пост {views['max']:,}".replace(",", " "))
        lines += parts
    lines += ["", f"Всего подписчиков: <b>{total_members:,}</b>".replace(",", " ")
              + f" ({'+' if total_growth >= 0 else ''}{total_growth} за неделю)"]
    text = "\n".join(lines)
    if ctx.secrets.admin_chat_id:
        ctx.bot.send_text(ctx.secrets.admin_chat_id, text)
    return text
