"""Контекст бота и сборка постов (текст + картинка, видео-репост, кросс-пиар, каталог)."""
from __future__ import annotations

import logging
import os

import requests
import random
from dataclasses import dataclass, field
from datetime import date, datetime

from . import images, reposter
from .ai import AI
from .settings import Channel, Network, Rubric, Secrets, load_channels, load_network
from .state import State
from .tg import Bot, file_id_of
from .util import esc, human_date, now, random_time_in_window, weighted_choice

log = logging.getLogger(__name__)

CAPTION_LIMIT = 1024   # лимит подписи к фото/видео в Telegram
SAFE_CAPTION = 950     # запас под кросс-пиар


@dataclass
class Context:
    network: Network
    channels: list[Channel]
    secrets: Secrets
    bot: Bot
    ai: AI
    state: State
    rng: random.Random = field(default_factory=random.Random)
    dry_run: bool = False

    @classmethod
    def from_env(cls) -> "Context":
        dry = os.environ.get("DRY_RUN", "") not in ("", "0", "false")
        secrets = Secrets.from_env()
        network = load_network()
        return cls(network=network, channels=load_channels(), secrets=secrets,
                   bot=Bot(secrets.bot_token, dry_run=dry), ai=AI(secrets, network, dry_run=dry),
                   state=State(), dry_run=dry)

    def channel(self, key: str) -> Channel | None:
        return next((c for c in self.channels if c.key == key), None)

    def ready_channels(self) -> list[Channel]:
        paused = set(self.state.meta()["paused"])
        if "all" in paused:
            return []
        return [c for c in self.channels if c.ready and c.key not in paused]


def visible_len(html_text: str) -> int:
    """Длина текста так, как её считает Telegram (без HTML-тегов)."""
    import re
    import html as _html
    return len(_html.unescape(re.sub(r"<[^>]+>", "", html_text)))


def with_signature(ch: Channel, text: str) -> str:
    return f"{text}\n\n{ch.signature}" if ch.signature else text


def store_media(ctx: Context, kind: str, media) -> str:
    """Загружает фото (URL или байты) или видео (файл) в служебный канал и возвращает file_id.
    file_id потом используется и для предпросмотра, и для публикации."""
    if not ctx.secrets.storage_chat_id:
        if kind == "video" or isinstance(media, (bytes, bytearray)):
            raise RuntimeError("для видео и сгенерированных картинок нужен STORAGE_CHAT_ID")
        return media  # фото можно публиковать прямо по URL
    if kind == "photo":
        msg = ctx.bot.send_photo(ctx.secrets.storage_chat_id, media)
    else:
        msg = ctx.bot.send_video(ctx.secrets.storage_chat_id, media)
    fid = file_id_of(msg, kind)
    if not fid:
        raise RuntimeError("Telegram не вернул file_id")
    return fid


def download_image(url: str) -> bytes | None:
    """Скачиваем картинку сами: Telegram по URL открывает не все сайты (например, Wikimedia)."""
    if not url.startswith("http"):
        return None
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": "channel-network-bot/1.0"})
        r.raise_for_status()
        if not r.headers.get("Content-Type", "").startswith("image/") or len(r.content) > 9_000_000:
            return None
        return r.content
    except requests.RequestException:
        return None


def build_text_post(ctx: Context, ch: Channel, rubric: Rubric, day: date, history: dict,
                    model: str | None = None) -> dict:
    gen = ctx.ai.generate_post(ch, rubric, human_date(day), history["topics"], model=model)
    raw_text = gen["text"]
    # текст должен поместиться в подпись к фото (лимит Telegram 1024 знака)
    if rubric.image and len(raw_text) + len(ch.signature) > SAFE_CAPTION - 60:
        try:
            shorter = ctx.ai.shorten(ch, raw_text, SAFE_CAPTION - 60 - len(ch.signature), model=model)
            if 40 <= len(shorter) < len(raw_text):
                raw_text = shorter
        except Exception as e:
            log.warning("не удалось сократить пост: %s", e)
    text = with_signature(ch, esc(raw_text))
    history["topics"].append(gen["topic"])
    post = {"kind": "text", "text": text, "media": None, "topic": gen["topic"], "source": None}
    if rubric.image:
        attach_image(ctx, ch, post, gen, history)
    return post


def generate_checked(ctx: Context, post: dict) -> tuple[bytes | None, bytes | None]:
    """GigaChat рисует, ИИ со зрением проверяет. Не подошло — перерисовка с уточнением.
    Возвращает (проверенная картинка, последняя непроверенная)."""
    prompt = post["image_prompt"]
    last = None
    attempts = max(1, ctx.network.gen_attempts)
    for attempt in range(attempts):
        data = images.generate_gigachat(prompt, ctx.secrets.gigachat_key, dry_run=ctx.dry_run)
        if not data:
            break
        last = data
        if not ctx.network.image_check:
            return data, None
        try:
            ok, fix = ctx.ai.check_generated(post.get("topic", ""), post["text"], post["image_prompt"], data)
        except Exception as e:  # проверка не сработала — не блокируем пост
            log.warning("проверка нарисованной картинки: %s", e)
            return data, None
        if ok:
            log.info("картинка GigaChat прошла проверку с попытки %d", attempt + 1)
            return data, None
        log.info("картинка GigaChat не подошла (%s), перерисовываю", fix)
        post.setdefault("image_notes", []).append(fix)
        if fix:
            prompt = f"{post['image_prompt']} Обязательно: {fix}"
    return None, last


def use_generated(ctx: Context, post: dict, data: bytes, via: str) -> bool:
    try:
        post["media"] = store_media(ctx, "photo", data)
    except Exception as e:
        log.warning("картинка GigaChat не загрузилась: %s", e)
        return False
    post["kind"] = "photo" if visible_len(post["text"]) <= CAPTION_LIMIT else "photo_text"
    post["image_from"] = "gigachat"
    post["image_via"] = via
    post["image_key"] = ""
    return True


def attach_image(ctx: Context, ch: Channel, post: dict, gen: dict, history: dict,
                 exclude: tuple = ()) -> None:
    """Картинка к посту. generate: сначала YandexART; затем бесплатные фотостоки
    (Pixabay → Pexels → Openverse) по запросам ИИ; если точного нет — тематическое фото канала."""
    source = ch.image_source or ctx.network.image_source
    post["image_prompt"] = gen.get("image_prompt") or post.get("image_prompt") or ""
    unchecked = None  # нарисованная картинка, не прошедшая проверку — на крайний случай
    if source == "gigachat" and post["image_prompt"] and ctx.secrets.gigachat_key:
        good, unchecked = generate_checked(ctx, post)
        if good and use_generated(ctx, post, good, "gen"):
            return
    if source == "generate" and gen.get("image_prompt"):
        data = images.generate_image(gen["image_prompt"], ctx.secrets.yandex_api_key,
                                     ctx.secrets.yandex_folder_id, ctx.network.art_model,
                                     ctx.network.image_aspect, dry_run=ctx.dry_run)
        if data:
            try:
                post["media"] = store_media(ctx, "photo", data)
                post["kind"] = "photo"
                post["image_from"] = "art"
                return
            except Exception as e:
                log.warning("сгенерированная картинка не загрузилась: %s", e)
    queries = gen.get("image_queries") or ([gen["image_query"]] if gen.get("image_query") else [])
    judge = None
    if ctx.network.image_check and not ctx.dry_run:
        topic, plain = post.get("topic", ""), post["text"]
        judge = lambda cands: ctx.ai.pick_image(topic, plain, cands)
    post["image_queries"] = queries
    candidates = images.find_candidates(
        queries, ch.image_fallback, ctx.secrets.pixabay_key, ctx.secrets.pexels_key,
        history["images"] + list(exclude), ctx.rng, category=ch.image_category, dry_run=ctx.dry_run,
        judge=judge)
    if unchecked and not any(c.via == "ai" for c in candidates[:1]):
        # фотостоки не нашли проверенного фото — лучше нарисованная, хоть и с замечаниями
        if use_generated(ctx, post, unchecked, "gen_warn"):
            return
    for cand in candidates:
        try:
            post["media"] = store_media(ctx, "photo", download_image(cand.url) or cand.url)
        except Exception as e:  # картинка недоступна — пробуем следующую
            log.warning("картинка не загрузилась (%s): %s", cand.url, e)
            continue
        post["image_from"] = cand.key.split(":", 1)[0]
        post["image_key"] = cand.key
        post["image_via"] = cand.via
        post["image_credit"] = cand.credit
        history["images"].append(cand.key)
        if cand.credit:
            post["text"] += f"\n\n{cand.credit}"
        # если подпись всё же длиннее лимита — фото и текст уйдут двумя сообщениями
        post["kind"] = "photo" if visible_len(post["text"]) <= CAPTION_LIMIT else "photo_text"
        return
    log.warning("пост %s остался без картинки: ни один фотосток не ответил", ch.key)


def build_video_post(ctx: Context, ch: Channel, video: dict, history: dict) -> dict | None:
    try:
        caption = esc(ctx.ai.video_caption(ch, video["text"]))
        src = esc(video["source"])
        caption = with_signature(ch, f"{caption}\n\n📎 Источник: {src}")
        fid = store_media(ctx, "video", video["path"])
    except Exception as e:
        log.warning("видео %s пропущено: %s", video.get("key"), e)
        return None
    finally:
        try:
            os.remove(video["path"])
        except (OSError, KeyError, TypeError):
            pass
    history["videos"].append(video["key"])
    return {"kind": "video", "text": caption, "media": fid, "topic": f"видео {video['source']}",
            "source": video["key"]}


def pick_rubrics(ch: Channel, n: int, rng: random.Random) -> list[Rubric]:
    pool = [r for r in ch.rubrics if r.type in ("text", "video")]
    if not pool:
        return []
    picked: list[Rubric] = []
    for i in range(n):
        allowed = [r for r in pool if r.slots is None or i in r.slots] or pool
        options = [r for r in allowed if not picked or r.name != picked[-1].name] or allowed
        picked.append(weighted_choice(options, [r.weight for r in options], rng))
    return picked


def fallback_text_rubric(ch: Channel, rng: random.Random) -> Rubric | None:
    texts = [r for r in ch.rubrics if r.type == "text"]
    return weighted_choice(texts, [r.weight for r in texts], rng) if texts else None


def plan_channel(ctx: Context, ch: Channel, day: date, errors: list[str]) -> list[dict]:
    rng = ctx.rng
    history = ctx.state.history(ch.key)
    slots = sorted(random_time_in_window(day, w, rng) for w in ch.windows)
    rubrics = pick_rubrics(ch, len(slots), rng)
    need_videos = sum(1 for r in rubrics if r.type == "video")
    videos = reposter.find_videos(ch.video_sources, history["videos"], need_videos,
                                  ctx.secrets, ctx.network, dry_run=ctx.dry_run)
    posts = []
    for i, (slot, rubric) in enumerate(zip(slots, rubrics)):
        post = None
        try:
            if rubric.type == "video" and videos:
                post = build_video_post(ctx, ch, videos.pop(0), history)
            if post is None:
                if rubric.type == "video":
                    rubric = fallback_text_rubric(ch, rng)
                    if rubric is None:
                        errors.append(f"{ch.title}: нет видео для слота {slot:%H:%M}")
                        continue
                post = build_text_post(ctx, ch, rubric, day, history)
        except Exception as e:
            log.exception("пост %s/%s", ch.key, rubric.name if rubric else "?")
            errors.append(f"{ch.title}, {rubric.name if rubric else 'пост'}: {str(e)[:150]}")
            continue
        post.update({"id": f"{ch.key}-{i + 1}", "channel": ch.key, "rubric": rubric.name,
                     "time": slot.isoformat(), "status": "pending", "auto": ch.auto_approve})
        posts.append(post)
    ctx.state.save_history(ch.key, history)
    return posts


def promo_line(target: Channel) -> str:
    tagline = f" — {esc(target.tagline)}" if target.tagline else ""
    return f'👉 {target.emoji} <a href="{esc(target.invite_link)}">{esc(target.title)}</a>{tagline}'


def add_promo(ctx: Context, posts: list[dict]) -> None:
    """Каждому каналу — по одной строке кросс-пиара в случайный пост, только внутри кластера."""
    ready = ctx.ready_channels()
    for ch in ready:
        targets = [c for c in ready if c.key != ch.key and c.cluster == ch.cluster and c.invite_link]
        own = [p for p in posts if p["channel"] == ch.key]
        if not targets or not own:
            continue
        for _ in range(ctx.network.promo_per_channel_per_day):
            line = promo_line(ctx.rng.choice(targets))
            fitting = [p for p in own if "👉" not in p["text"] and
                       (p["kind"] in ("text", "photo_text") or
                        visible_len(p["text"]) + visible_len(line) + 2 <= CAPTION_LIMIT)]
            if not fitting:
                break
            post = ctx.rng.choice(fitting)
            post["text"] += f"\n\n{line}"


def catalog_posts(ctx: Context, day: date) -> list[dict]:
    """Еженедельный пост «Каналы нашей сети» в каждом канале — только свой кластер."""
    ready = ctx.ready_channels()
    posts = []
    for ch in ready:
        others = [c for c in ready if c.key != ch.key and c.cluster == ch.cluster and c.invite_link]
        if not others:
            continue
        lines = [promo_line(c).replace("👉 ", "") for c in others]
        text = f"📚 <b>{esc(ctx.network.name)}: наши каналы</b>\n\n" + "\n".join(lines)
        slot = random_time_in_window(day, ctx.network.catalog_window, ctx.rng)
        post = {"id": f"{ch.key}-catalog", "channel": ch.key, "rubric": "Каталог сети",
                "time": slot.isoformat(), "status": "pending", "auto": True, "kind": "text",
                "text": text, "media": None, "topic": "каталог", "source": None}
        history = ctx.state.history(ch.key)
        attach_image(ctx, ch, post, {"image_queries": []}, history)
        ctx.state.save_history(ch.key, history)
        posts.append(post)
    return posts


def replace_image(ctx: Context, post: dict) -> dict | None:
    """Кнопка 🖼: другая картинка к тому же тексту."""
    ch = ctx.channel(post["channel"])
    if not ch or post["kind"] not in ("photo", "photo_text", "text"):
        return None
    new = dict(post)
    credit = post.get("image_credit") or ""
    if credit and new["text"].endswith(f"\n\n{credit}"):
        new["text"] = new["text"][: -len(credit) - 2]
    for k in ("media", "image_from", "image_key", "image_via", "image_credit"):
        new.pop(k, None)
    new["kind"] = "text"
    history = ctx.state.history(ch.key)
    queries = post.get("image_queries") or []
    exclude = tuple(k for k in [post.get("image_key")] if k)
    attach_image(ctx, ch, new, {"image_queries": queries, "image_prompt": post.get("image_prompt", "")},
                 history, exclude=exclude)
    ctx.state.save_history(ch.key, history)
    if new["kind"] == "text":
        return None
    new["status"] = "pending"
    return new


def rebuild_post(ctx: Context, post: dict) -> dict | None:
    """Пересоздаёт пост (кнопка 🔄) с тем же временем и каналом."""
    ch = ctx.channel(post["channel"])
    if not ch:
        return None
    day = datetime.fromisoformat(post["time"]).date()
    history = ctx.state.history(ch.key)
    new = None
    if post["kind"] == "video":
        videos = reposter.find_videos(ch.video_sources, history["videos"], 1, ctx.secrets,
                                      ctx.network, dry_run=ctx.dry_run)
        if videos:
            new = build_video_post(ctx, ch, videos[0], history)
    if new is None:
        rubric = next((r for r in ch.rubrics if r.name == post["rubric"] and r.type == "text"), None)
        rubric = rubric or fallback_text_rubric(ch, ctx.rng)
        if rubric is None:
            return None
        new = build_text_post(ctx, ch, rubric, day, history)
        new["rubric"] = rubric.name
    ctx.state.save_history(ch.key, history)
    for k in ("id", "channel", "time", "auto"):
        new[k] = post[k]
    new.setdefault("rubric", post["rubric"])
    new["status"] = "pending"
    return new


def deadline_passed(queue: dict) -> bool:
    return now() >= datetime.fromisoformat(queue["deadline"])
