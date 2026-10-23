"""Картинки к постам.

Бесплатные фотостоки (по порядку): Pixabay → Pexels → Openverse (только CC0 / Public Domain).
По каждому запросу выбирается фото, у которого теги/описание совпадают с запросом.
Если точного совпадения нет нигде — берётся тематическое фото канала (fallback), чтобы
у каждого поста была картинка.

Платная опция: генерация YandexART (image_source: generate), при неудаче — те же фотостоки.
"""
from __future__ import annotations

import base64
import logging
import random
import re
import time
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

PIXABAY_URL = "https://pixabay.com/api/"
PEXELS_URL = "https://api.pexels.com/v1/search"
OPENVERSE_URL = "https://api.openverse.org/v1/images/"
ART_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/imageGenerationAsync"
OPERATION_URL = "https://llm.api.cloud.yandex.net:443/operations/{}"
UA = {"User-Agent": "channel-network-bot/1.0 (Telegram channels)"}

ART_STYLE = ("Фотореалистичная фотография, естественный свет, чёткий фокус на главном предмете. "
             "Без текста, надписей, логотипов и водяных знаков.")


@dataclass
class Candidate:
    key: str          # уникальный id, например "pixabay:123" — чтобы не повторяться в канале
    url: str
    score: int        # сколько слов запроса совпало с тегами/описанием
    credit: str = ""  # подпись источника, если её просит сервис


# ─────────────── генерация YandexART (платно) ───────────────
def generate_image(prompt: str, api_key: str, folder_id: str, model: str = "yandex-art/latest",
                   aspect: str = "16:9", timeout_s: int = 120, dry_run: bool = False) -> bytes | None:
    """Возвращает JPEG-байты сгенерированной картинки или None (тогда используем фотостоки)."""
    if not prompt or not api_key or not folder_id or dry_run:
        return None
    w, _, h = aspect.partition(":")
    headers = {"Authorization": f"Api-Key {api_key}", "x-folder-id": folder_id}
    body = {
        "modelUri": model if model.startswith("art://") else f"art://{folder_id}/{model}",
        "messages": [{"weight": "1", "text": f"{prompt.strip()}. {ART_STYLE}"[:500]}],
        "generationOptions": {"seed": str(random.randint(1, 2**31 - 1)),
                              "aspectRatio": {"widthRatio": w or "16", "heightRatio": h or "9"}},
    }
    try:
        resp = requests.post(ART_URL, headers=headers, json=body, timeout=60)
        if resp.status_code != 200:
            log.warning("YandexART HTTP %s: %s", resp.status_code, resp.text[:300])
            return None
        op_id = resp.json()["id"]
        deadline = time.time() + timeout_s
        time.sleep(8)
        while time.time() < deadline:
            r = requests.get(OPERATION_URL.format(op_id), headers=headers, timeout=30)
            data = r.json()
            if data.get("done"):
                if data.get("error"):
                    log.warning("YandexART ошибка: %s", data["error"])
                    return None
                return base64.b64decode(data["response"]["image"])
            time.sleep(5)
        log.warning("YandexART: не дождались картинки за %s с", timeout_s)
    except (requests.RequestException, ValueError, KeyError) as e:
        log.warning("YandexART: %s", e)
    return None


# ─────────────── оценка совпадения ───────────────
def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", (text or "").lower()) if len(w) > 2}


def _score(text: str, query_words: set[str]) -> int:
    tags = _words(text)
    # совпадение слова или его начала (wiper ~ wipers, frozen ~ froze)
    return sum(1 for q in query_words if any(t.startswith(q[:5]) or q.startswith(t[:5]) for t in tags))


# ─────────────── фотостоки ───────────────
def search_pixabay(query: str, api_key: str, category: str = "") -> list[Candidate]:
    if not api_key or not query:
        return []
    params = {"key": api_key, "q": query[:100], "image_type": "photo", "safesearch": "true",
              "orientation": "horizontal", "per_page": 50}
    if category:
        params["category"] = category
    try:
        resp = requests.get(PIXABAY_URL, params=params, timeout=30)
        resp.raise_for_status()
        hits = resp.json().get("hits", [])
    except (requests.RequestException, ValueError) as e:
        log.warning("Pixabay '%s': %s", query, e)
        return []
    qw = _words(query)
    return [Candidate(f"pixabay:{h['id']}", h.get("largeImageURL") or h["webformatURL"],
                      _score(h.get("tags", ""), qw)) for h in hits if h.get("webformatURL")]


def search_pexels(query: str, api_key: str) -> list[Candidate]:
    if not api_key or not query:
        return []
    try:
        resp = requests.get(PEXELS_URL, headers={"Authorization": api_key, **UA},
                            params={"query": query[:100], "orientation": "landscape", "per_page": 40},
                            timeout=30)
        resp.raise_for_status()
        photos = resp.json().get("photos", [])
    except (requests.RequestException, ValueError) as e:
        log.warning("Pexels '%s': %s", query, e)
        return []
    qw = _words(query)
    return [Candidate(f"pexels:{p['id']}", p["src"].get("large2x") or p["src"]["large"],
                      _score(p.get("alt", ""), qw), credit="📷 Pexels") for p in photos if p.get("src")]


def search_openverse(query: str) -> list[Candidate]:
    """Только CC0 и Public Domain — подпись автора не требуется."""
    if not query:
        return []
    try:
        resp = requests.get(OPENVERSE_URL, headers=UA, timeout=30, params={
            "q": query[:100], "license": "cc0,pdm", "page_size": 30, "mature": "false",
            "aspect_ratio": "wide"})
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except (requests.RequestException, ValueError) as e:
        log.warning("Openverse '%s': %s", query, e)
        return []
    qw = _words(query)
    out = []
    for r in results:
        text = " ".join([r.get("title") or ""] + [t.get("name", "") for t in r.get("tags") or []])
        if r.get("url"):
            out.append(Candidate(f"openverse:{r['id']}", r["url"], _score(text, qw)))
    return out


def _sources(pixabay_key: str, pexels_key: str, category: str):
    return [
        ("pixabay", lambda q: search_pixabay(q, pixabay_key, category)),
        ("pexels", lambda q: search_pexels(q, pexels_key)),
        ("openverse", search_openverse),
    ]


def find_candidates(queries: list[str], fallback_query: str, pixabay_key: str, pexels_key: str,
                    used: list[str], rng: random.Random, category: str = "",
                    dry_run: bool = False, limit: int = 5) -> list[Candidate]:
    """Подходящие картинки в порядке предпочтения (несколько — на случай, если Telegram
    не сможет скачать первую). Сначала точные совпадения по запросам ИИ, потом тематическое
    фото канала (fallback_query) без строгой проверки тегов."""
    if dry_run:
        return []
    used_set = set(used)
    result: list[Candidate] = []
    seen: set[str] = set()

    def take(cands: list[Candidate], need_match: bool) -> None:
        fresh = [c for c in cands if c.key not in used_set and c.key not in seen]
        if need_match:
            best = max((c.score for c in fresh), default=0)
            if best == 0:
                return
            fresh = [c for c in fresh if c.score == best]
        pick = fresh[:6]
        rng.shuffle(pick)
        for c in pick[:2]:
            seen.add(c.key)
            result.append(c)

    for q in [q for q in queries if q and q.strip()][:3]:
        for name, search in _sources(pixabay_key, pexels_key, category):
            take(search(q), need_match=True)
            if len(result) >= limit:
                return result[:limit]
        if result:
            return result  # нашлось точное совпадение — общие запросы не нужны
    # ничего точного: тематическое фото канала, чтобы пост не остался без картинки
    for q in [fallback_query, (fallback_query or "").split(" ")[0]]:
        if not q:
            continue
        for name, search in _sources(pixabay_key, pexels_key, category):
            take(search(q), need_match=False)
            if len(result) >= limit:
                return result[:limit]
        if result:
            break
    return result[:limit]


def find_image(query: str, api_key: str, used: list[str], rng: random.Random,
               dry_run: bool = False, category: str = "") -> tuple[str, str] | None:
    """Совместимость со старым кодом: один запрос, только Pixabay, строгое совпадение тегов."""
    if dry_run or not query or not api_key:
        return None
    cands = [c for c in search_pixabay(query, api_key, category) if c.key not in set(used)]
    best = max((c.score for c in cands), default=0)
    if not cands or best == 0:
        return None
    top = [c for c in cands if c.score == best][:5]
    c = rng.choice(top)
    return c.key.split(":", 1)[1], c.url
