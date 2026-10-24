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
WIKIMEDIA_URL = "https://commons.wikimedia.org/w/api.php"
ART_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/imageGenerationAsync"
OPERATION_URL = "https://llm.api.cloud.yandex.net:443/operations/{}"
UA = {"User-Agent": "channel-network-bot/1.0 (Telegram channels; contact via GitHub)"}
# лицензии Wikimedia/Openverse, которые разрешают коммерческое использование
FREE_LICENSES = ("public domain", "pd", "cc0", "cc by", "cc-by", "by", "by-sa")

ART_STYLE = ("Фотореалистичная фотография, естественный свет, чёткий фокус на главном предмете. "
             "Без текста, надписей, логотипов и водяных знаков.")


@dataclass
class Candidate:
    key: str          # уникальный id, например "pixabay:123" — чтобы не повторяться в канале
    url: str
    score: int        # сколько слов запроса совпало с тегами/описанием
    credit: str = ""  # подпись источника/автора, если её требует лицензия или сервис
    preview: str = "" # уменьшенная копия — для проверки картинки ИИ
    ratio: float = 0.0  # доля слов запроса, найденных в тегах (0…1)
    query_index: int = 0  # номер запроса ИИ, по которому найдено фото (0 — главная сцена)
    via: str = ""     # как выбрано: ai — проверено ИИ, tags — по тегам, fallback — тематическое фото


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


def _cand(key: str, url: str, text: str, qw: set[str], credit: str = "", preview: str = "") -> Candidate:
    score = _score(text, qw)
    return Candidate(key, url, score, credit=credit, preview=preview or url,
                     ratio=score / max(len(qw), 1))


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text or "")).strip()


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
    return [_cand(f"pixabay:{h['id']}", h.get("largeImageURL") or h["webformatURL"], h.get("tags", ""),
                  qw, preview=h["webformatURL"]) for h in hits if h.get("webformatURL")]


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
    return [_cand(f"pexels:{p['id']}", p["src"].get("large2x") or p["src"]["large"], p.get("alt", ""), qw,
                  credit="📷 Pexels", preview=p["src"].get("medium", "")) for p in photos if p.get("src")]


def search_openverse(query: str) -> list[Candidate]:
    """Фото с лицензиями для коммерческого использования (CC0, Public Domain, CC BY, CC BY-SA).
    Для CC BY / BY-SA в пост добавляется подпись автора и лицензии."""
    if not query:
        return []
    try:
        resp = requests.get(OPENVERSE_URL, headers=UA, timeout=30, params={
            "q": query[:100], "license_type": "commercial", "page_size": 30, "mature": "false"})
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except (requests.RequestException, ValueError) as e:
        log.warning("Openverse '%s': %s", query, e)
        return []
    qw = _words(query)
    out = []
    for r in results:
        lic = (r.get("license") or "").lower()
        if lic not in ("cc0", "pdm", "by", "by-sa") or not r.get("url"):
            continue
        text = " ".join([r.get("title") or ""] + [t.get("name", "") for t in r.get("tags") or []])
        credit = ""
        if lic in ("by", "by-sa"):
            author = (r.get("creator") or "автор").strip()[:60]
            credit = f"📷 {author}, CC {lic.upper()} {r.get('license_version') or ''}".strip()
        out.append(_cand(f"openverse:{r['id']}", r["url"], text, qw, credit=credit,
                         preview=r.get("thumbnail") or r["url"]))
    return out


def search_wikimedia(query: str) -> list[Candidate]:
    """Wikimedia Commons — много технических фото (узлы авто, инструменты, история).
    Берём только свободные лицензии; для CC BY / BY-SA — подпись автора."""
    if not query:
        return []
    try:
        resp = requests.get(WIKIMEDIA_URL, headers=UA, timeout=30, params={
            "action": "query", "format": "json", "generator": "search", "gsrnamespace": 6,
            "gsrsearch": f"{query[:100]} filetype:bitmap", "gsrlimit": 30,
            "prop": "imageinfo", "iiprop": "url|extmetadata|mime|size", "iiurlwidth": 1280})
        resp.raise_for_status()
        pages = (resp.json().get("query") or {}).get("pages") or {}
    except (requests.RequestException, ValueError) as e:
        log.warning("Wikimedia '%s': %s", query, e)
        return []
    qw = _words(query)
    out = []
    for page in sorted(pages.values(), key=lambda p: p.get("index", 0)):
        info = (page.get("imageinfo") or [{}])[0]
        if info.get("mime") not in ("image/jpeg", "image/png") or (info.get("width") or 0) < 600:
            continue
        meta = info.get("extmetadata") or {}
        lic = _strip_html((meta.get("LicenseShortName") or {}).get("value", ""))
        low = lic.lower()
        if not low or not any(low.startswith(f) for f in FREE_LICENSES) or "nc" in low.split("-"):
            continue
        title = page.get("title", "").removeprefix("File:")
        desc = _strip_html((meta.get("ImageDescription") or {}).get("value", ""))[:300]
        credit = ""
        if not (low.startswith("public domain") or low.startswith("pd") or low.startswith("cc0")):
            author = _strip_html((meta.get("Artist") or {}).get("value", "")) or "автор"
            credit = f"📷 {author[:60]}, {lic} / Wikimedia Commons"
        url = info.get("thumburl") or info.get("url")
        if url:
            out.append(_cand(f"wikimedia:{page.get('pageid')}", url, f"{title} {desc}", qw,
                             credit=credit, preview=url))
    return out


SOURCE_RANK = {"pixabay": 0, "pexels": 1, "wikimedia": 2, "openverse": 3}


def _sources(pixabay_key: str, pexels_key: str, category: str):
    return [
        ("pixabay", lambda q: search_pixabay(q, pixabay_key, category)),
        ("pexels", lambda q: search_pexels(q, pexels_key)),
        ("wikimedia", search_wikimedia),
        ("openverse", search_openverse),
    ]


def _shortlists(buckets: list[list[Candidate]], size: int = 8) -> list[list[Candidate]]:
    """Делит найденные фото на подборки для ИИ: в каждой больше всего фото по главному запросу,
    но есть и по остальным — чтобы ИИ было из чего выбрать."""
    quotas = [3, 2, 2, 1]
    pools = [list(b) for b in buckets]
    lists = []
    while any(pools) and len(lists) < 2:
        batch: list[Candidate] = []
        for i, pool in enumerate(pools):
            take = quotas[i] if i < len(quotas) else 1
            batch += pool[:take]
            del pool[:take]
        # если каких-то запросов не хватило — добираем из остальных по порядку
        for pool in pools:
            while pool and len(batch) < size:
                batch.append(pool.pop(0))
        if batch:
            lists.append(batch[:size])
    return lists


def find_candidates(queries: list[str], fallback_query: str, pixabay_key: str, pexels_key: str,
                    used: list[str], rng: random.Random, category: str = "",
                    dry_run: bool = False, limit: int = 5, judge=None) -> list[Candidate]:
    """Подходящие картинки в порядке предпочтения.

    1. Запросы ИИ идут от главной сцены поста к синонимам. Фото по первому запросу всегда важнее фото
       по следующим; внутри запроса — у кого в тегах/описании больше слов запроса.
    2. Если задан judge (ИИ со зрением), он смотрит подборку из 8 фото (больше всего — по главной сцене)
       и выбирает подходящее. Не подошло ни одно — вторая подборка. Снова нет — тематическое фото канала.
    3. Без judge — берём лучшее по порядку запросов и тегам.
    """
    if dry_run:
        return []
    used_set = set(used)
    seen: set[str] = set()
    buckets: list[list[Candidate]] = []
    for qi, q in enumerate([q for q in queries if q and q.strip()][:4]):
        bucket = []
        for name, search in _sources(pixabay_key, pexels_key, category):
            for c in search(q):
                if c.key in used_set or c.key in seen or c.score == 0:
                    continue
                c.query_index = qi
                seen.add(c.key)
                bucket.append(c)
        bucket.sort(key=lambda c: (-c.score, SOURCE_RANK.get(c.key.split(":")[0], 9), rng.random()))
        buckets.append(bucket)
    ranked = [c for b in buckets for c in b]

    if ranked and judge:
        judge_failed = False
        for shortlist in _shortlists(buckets):
            try:
                best = judge(shortlist)
            except Exception as e:  # проверка ИИ — улучшение, а не обязательный шаг
                log.warning("проверка картинок ИИ: %s", e)
                judge_failed = True
                break
            if best is not None and 0 <= best < len(shortlist):
                chosen = shortlist[best]
                chosen.via = "ai"
                return [chosen] + [c for c in ranked if c.key != chosen.key][:limit - 1]
        if not judge_failed:
            ranked = []  # ИИ отверг все подборки — к тематическому фото
    if ranked:
        for c in ranked:
            c.via = "tags"
        return ranked[:limit]
    # ничего подходящего: тематическое фото канала
    result: list[Candidate] = []
    for q in [fallback_query, (fallback_query or "").split(" ")[0]]:
        if not q:
            continue
        for name, search in _sources(pixabay_key, pexels_key, category):
            fresh = [c for c in search(q) if c.key not in used_set]
            best = max((c.score for c in fresh), default=0)
            pick = [c for c in fresh if c.score == best][:6]  # сначала лучше совпавшие с темой канала
            rng.shuffle(pick)
            for c in pick[:2]:
                c.via = "fallback"
            result += pick[:2]
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
