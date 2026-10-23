"""Подбор картинок с Pixabay (бесплатная лицензия, без указания автора)."""
from __future__ import annotations

import logging
import random

import requests

log = logging.getLogger(__name__)

PIXABAY_URL = "https://pixabay.com/api/"


def find_image(query: str, api_key: str, used: list[str], rng: random.Random,
               dry_run: bool = False) -> tuple[str, str] | None:
    """Возвращает (id, url) картинки, которой ещё не было в канале, или None."""
    if not query or not api_key or dry_run:
        return None
    try:
        resp = requests.get(PIXABAY_URL, params={
            "key": api_key, "q": query[:100], "image_type": "photo", "safesearch": "true",
            "orientation": "horizontal", "per_page": 30, "order": "popular"}, timeout=30)
        resp.raise_for_status()
        hits = [h for h in resp.json().get("hits", []) if str(h["id"]) not in used]
    except (requests.RequestException, ValueError, KeyError) as e:
        log.warning("Pixabay '%s': %s", query, e)
        return None
    if not hits:
        # пробуем запрос короче: первое слово
        short = query.split()[0] if " " in query else ""
        return find_image(short, api_key, used, rng) if short else None
    hit = rng.choice(hits[:10])
    return str(hit["id"]), hit.get("largeImageURL") or hit["webformatURL"]
