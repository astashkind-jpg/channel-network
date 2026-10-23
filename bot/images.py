"""Картинки к постам.

1) Генерация в YandexART (Yandex AI Studio) — рисует сцену точно под текст поста.
2) Запасной вариант — поиск на Pixabay (бесплатная лицензия, без указания автора):
   выбирается фото, у которого теги лучше всего совпадают с запросом.
"""
from __future__ import annotations

import base64
import logging
import random
import re
import time

import requests

log = logging.getLogger(__name__)

PIXABAY_URL = "https://pixabay.com/api/"
ART_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/imageGenerationAsync"
OPERATION_URL = "https://llm.api.cloud.yandex.net:443/operations/{}"

ART_STYLE = ("Фотореалистичная фотография, естественный свет, чёткий фокус на главном предмете. "
             "Без текста, надписей, логотипов и водяных знаков.")


# ─────────────── генерация YandexART ───────────────
def generate_image(prompt: str, api_key: str, folder_id: str, model: str = "yandex-art/latest",
                   aspect: str = "16:9", timeout_s: int = 120, dry_run: bool = False) -> bytes | None:
    """Возвращает JPEG-байты сгенерированной картинки или None (тогда используем Pixabay)."""
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


# ─────────────── поиск Pixabay ───────────────
def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", (text or "").lower()) if len(w) > 2}


def _score(hit: dict, query_words: set[str]) -> int:
    tags = _words(hit.get("tags", ""))
    # совпадение слова или его начала (wiper ~ wipers)
    return sum(1 for q in query_words if any(t.startswith(q[:5]) for t in tags))


def find_image(query: str, api_key: str, used: list[str], rng: random.Random,
               dry_run: bool = False, category: str = "") -> tuple[str, str] | None:
    """Возвращает (id, url) самой подходящей по тегам картинки, которой ещё не было в канале."""
    if not query or not api_key or dry_run:
        return None
    qwords = _words(query)
    params = {"key": api_key, "q": query[:100], "image_type": "photo", "safesearch": "true",
              "orientation": "horizontal", "per_page": 50}
    if category:
        params["category"] = category
    try:
        resp = requests.get(PIXABAY_URL, params=params, timeout=30)
        resp.raise_for_status()
        hits = [h for h in resp.json().get("hits", []) if str(h["id"]) not in used]
    except (requests.RequestException, ValueError, KeyError) as e:
        log.warning("Pixabay '%s': %s", query, e)
        return None
    if not hits:
        if category:  # без категории выбор шире
            return find_image(query, api_key, used, rng, category="")
        words = query.split()
        return find_image(" ".join(words[:-1]), api_key, used, rng) if len(words) > 1 else None
    best = max(_score(h, qwords) for h in hits)
    # если ни один тег не совпал — фото, скорее всего, не по теме: лучше пост без картинки
    if qwords and best == 0:
        log.info("Pixabay '%s': нет фото с подходящими тегами", query)
        return None
    top = [h for h in hits if _score(h, qwords) == best][:5]
    hit = rng.choice(top)
    return str(hit["id"]), hit.get("largeImageURL") or hit["webformatURL"]
