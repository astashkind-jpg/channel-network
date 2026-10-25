"""Бесплатные стоковые видео Pixabay (тот же ключ PIXABAY_KEY, что и для фото).

Лицензия Pixabay разрешает публиковать ролики без подписи автора и без оплаты.
Ролик подбирается под тему поста: ИИ со зрением смотрит на обложки клипов и выбирает подходящий.
"""
from __future__ import annotations

import logging
import os
import tempfile

import requests

from .images import Candidate, _words

log = logging.getLogger(__name__)

PIXABAY_VIDEO_URL = "https://pixabay.com/api/videos/"
UA = {"User-Agent": "channel-network-bot/1.0"}


def _pick_file(videos: dict, max_bytes: int) -> dict | None:
    """Из вариантов large/medium/small/tiny берём самый качественный, который влезает в лимит
    и не больше Full HD (большие файлы долго грузятся и не нужны в Telegram)."""
    for size in ("medium", "large", "small", "tiny"):
        v = videos.get(size) or {}
        if v.get("url") and 0 < (v.get("size") or 0) <= max_bytes and (v.get("width") or 0) <= 1920:
            return v
    return None


def search_pixabay_videos(query: str, api_key: str, max_bytes: int, category: str = "",
                          min_sec: int = 5, max_sec: int = 60) -> list[Candidate]:
    if not api_key or not query:
        return []
    params = {"key": api_key, "q": query[:100], "video_type": "film", "safesearch": "true", "per_page": 30}
    if category:
        params["category"] = category
    try:
        resp = requests.get(PIXABAY_VIDEO_URL, params=params, timeout=30, headers=UA)
        resp.raise_for_status()
        hits = resp.json().get("hits", [])
    except (requests.RequestException, ValueError) as e:
        log.warning("Pixabay видео '%s': %s", query, e)
        return []
    qw = _words(query)
    result = []
    for h in hits:
        if not (min_sec <= (h.get("duration") or 0) <= max_sec):
            continue
        f = _pick_file(h.get("videos") or {}, max_bytes)
        if not f:
            continue
        tags = _words(h.get("tags", ""))
        score = len(qw & tags)
        result.append(Candidate(key=f"pixabayvideo:{h['id']}", url=f["url"], score=score,
                                preview=f.get("thumbnail") or "",
                                ratio=score / max(1, len(qw))))
    return result


def find_stock_videos(queries: list[str], fallback: str, api_key: str, used: list[str], max_bytes: int,
                      category: str = "", limit: int = 8) -> list[Candidate]:
    """Кандидаты по запросам ИИ (сначала самые точные), без уже использованных в канале."""
    seen = set(used)
    out: list[Candidate] = []
    qs = [q for q in queries if q]
    for qi, q in enumerate(qs + ([fallback] if fallback else [])):
        cands = search_pixabay_videos(q, api_key, max_bytes, category)
        if not cands and category:
            cands = search_pixabay_videos(q, api_key, max_bytes)
        cands.sort(key=lambda c: (c.score, c.ratio), reverse=True)
        for c in cands:
            if c.key in seen:
                continue
            seen.add(c.key)
            c.query_index = qi
            c.via = "fallback" if qi == len(qs) else "tags"
            out.append(c)
            if len(out) >= limit:
                return out
    return out


def download_video(url: str, max_bytes: int) -> str | None:
    """Скачивает ролик во временный файл; возвращает путь или None."""
    try:
        with requests.get(url, stream=True, timeout=120, headers=UA) as r:
            r.raise_for_status()
            fd, path = tempfile.mkstemp(suffix=".mp4", prefix="stock_")
            size = 0
            with os.fdopen(fd, "wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("ролик больше лимита")
                    fh.write(chunk)
        return path
    except (requests.RequestException, ValueError, OSError) as e:
        log.warning("видео не скачалось (%s): %s", url, e)
        try:
            os.remove(path)  # type: ignore[possibly-undefined]
        except (OSError, NameError, UnboundLocalError):
            pass
        return None
