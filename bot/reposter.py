"""Поиск видео в чужих Telegram-каналах для репоста со ссылкой на источник.

Работает через Telethon от имени пользовательского аккаунта (бот не может читать чужие каналы).
Нужны TG_API_ID, TG_API_HASH и TG_SESSION (см. scripts/make_session.py).
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
from datetime import datetime, timedelta, timezone

from .settings import Network, Secrets

log = logging.getLogger(__name__)


def _is_ad(text: str, markers: list[str]) -> bool:
    low = (text or "").lower()
    if any(m.lower() in low for m in markers):
        return True
    # ссылки-приглашения в чужие каналы почти всегда означают рекламу
    return "t.me/+" in low or "t.me/joinchat" in low


def find_videos(sources: list[str], used: list[str], count: int, secrets: Secrets,
                network: Network, dry_run: bool = False) -> list[dict]:
    """Скачивает до count самых просматриваемых свежих видео. Возвращает
    [{"source": "@канал", "key": "@канал:123", "text": подпись, "path": файл, "views": N}]"""
    if count <= 0 or not sources or dry_run:
        return []
    if not (secrets.tg_api_id and secrets.tg_api_hash and secrets.tg_session):
        log.warning("репост видео выключен: нет TG_API_ID / TG_API_HASH / TG_SESSION")
        return []
    try:
        return asyncio.run(_find(sources, set(used), count, secrets, network))
    except Exception as e:  # сеть, бан источника, протухшая сессия — день не должен падать
        log.error("репост видео: %s", e)
        return []


async def _find(sources, used, count, secrets, network) -> list[dict]:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    since = datetime.now(timezone.utc) - timedelta(days=network.video_max_age_days)
    max_bytes = network.video_max_mb * 1024 * 1024
    candidates = []
    async with TelegramClient(StringSession(secrets.tg_session), int(secrets.tg_api_id),
                              secrets.tg_api_hash) as client:
        for src in sources:
            try:
                async for m in client.iter_messages(src, limit=80):
                    if m.date < since:
                        break
                    if not m.video or m.video_note or not m.file or (m.file.size or 0) > max_bytes:
                        continue
                    key = f"{src}:{m.id}"
                    if key in used or _is_ad(m.message or "", network.ad_markers):
                        continue
                    candidates.append((m.views or 0, src, m))
            except Exception as e:
                log.warning("источник %s недоступен: %s", src, e)
        candidates.sort(key=lambda c: c[0], reverse=True)
        result = []
        tmpdir = tempfile.mkdtemp(prefix="videos_")
        for views, src, m in candidates:
            if len(result) >= count:
                break
            try:
                path = await client.download_media(m, file=f"{tmpdir}/{src.strip('@')}_{m.id}.mp4")
            except Exception as e:
                log.warning("не скачалось %s:%s: %s", src, m.id, e)
                continue
            result.append({"source": src, "key": f"{src}:{m.id}", "text": m.message or "",
                           "path": str(path), "views": views})
        return result


def channel_views(chat: str, days: int, secrets: Secrets) -> dict | None:
    """Средние просмотры постов своего канала за последние days дней (для отчёта)."""
    if not (secrets.tg_api_id and secrets.tg_api_hash and secrets.tg_session) or not chat:
        return None
    try:
        return asyncio.run(_views(chat, days, secrets))
    except Exception as e:
        log.warning("просмотры %s: %s", chat, e)
        return None


async def _views(chat, days, secrets):
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    since = datetime.now(timezone.utc) - timedelta(days=days)
    views = []
    async with TelegramClient(StringSession(secrets.tg_session), int(secrets.tg_api_id),
                              secrets.tg_api_hash) as client:
        target = int(chat) if chat.lstrip("-").isdigit() else chat
        async for m in client.iter_messages(target, limit=200):
            if m.date < since:
                break
            if m.views is not None:
                views.append(m.views)
    if not views:
        return {"posts": 0, "avg": 0, "max": 0}
    return {"posts": len(views), "avg": round(sum(views) / len(views)), "max": max(views)}
