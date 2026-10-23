"""Контекст бота и сборка постов (текст + картинка, видео-репост, кросс-пиар, каталог)."""
from __future__ import annotations

import logging
import os
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


def store_media(ctx: Context, kind: str, media: str) -> str:
    """Загружает фото (URL) или видео (файл) в служебный канал и возвращает file_id.
    file_id потом используется и для предпросмотра, и для публикации."""
    if not ctx.secrets.storage_chat_id:
        if kind == "video":
            raise RuntimeError("для видео нужен STORAGE_CHAT_ID (служебный канал)")
        return media  # фото можно публиковать прямо по URL
    if kind == "photo":
        msg = ctx.bot.send_photo(ctx.secrets.storage_chat_id, media)
    else:
        msg = ctx.bot.send_video(ctx.secrets.storage_chat_id, media)
    fid = file_id_of(msg, kind)
    if not fid:
        raise RuntimeError("Telegram не вернул file_id")
    return fid


def build_text_post(ctx: Context, ch: Channel, rubric: Rubric, day: date, history: dict,
                    model: str | None = None) -> dict:
    gen = ctx.ai.generate_post(ch, rubric, human_date(day), history["topics"], model=model)
    text = with_signature(ch, esc(gen["text"]))
    history["topics"].append(gen["topic"])
    post = {"kind": "text", "text": text, "media": None, "topic": gen["topic"], "source": None}
    if rubric.image and gen["image_query"] and visible_len(text) <= SAFE_CAPTION:
        found = images.find_image(gen["image_query"], ctx.secrets.pixabay_key, history["images"],
                                  ctx.rng, dry_run=ctx.dry_run)
        if found:
            img_id, url = found
            try:
                post["media"] = store_media(ctx, "photo", url)
                post["kind"] = "photo"
                history["images"].append(img_id)
            except Exception as e:
                log.warning("картинка не загрузилась (%s): %s", url, e)
    return post


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
                       (p["kind"] == "text" or visible_len(p["text"]) + visible_len(line) + 2 <= CAPTION_LIMIT)]
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
        posts.append({"id": f"{ch.key}-catalog", "channel": ch.key, "rubric": "Каталог сети",
                      "time": slot.isoformat(), "status": "pending", "auto": True, "kind": "text",
                      "text": text, "media": None, "topic": "каталог", "source": None})
    return posts


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
