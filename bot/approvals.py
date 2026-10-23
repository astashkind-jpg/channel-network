"""Обработка кнопок и команд администратора."""
from __future__ import annotations

import logging
from datetime import date, datetime

from .core import Context, rebuild_post
from .tg import TelegramError, keyboard
from .util import esc, now, short_date

log = logging.getLogger(__name__)

HELP = """🤖 <b>Бот сети каналов</b>

Каждый вечер я присылаю план на завтра. Если ничего не нажимать — посты выйдут сами (кроме каналов с ручным ✅).

Команды:
/status — что запланировано и опубликовано
/channels — список каналов и их готовность
/pause <i>ключ</i> или /pause all — остановить публикации
/resume <i>ключ</i> или /resume all — возобновить
/preview <i>ключ</i> — пробный пост канала (не публикуется)
/preview <i>ключ модель</i> — пробный пост другой моделью, например
<code>/preview auto qwen3.6-35b-a3b</code>"""

STATUS_ICON = {"pending": "⏳", "approved": "✅", "rejected": "❌", "published": "📤",
               "failed": "⚠️", "expired": "⌛", "replaced": "🔄"}


def post_buttons(day: str, pid: str) -> dict:
    return keyboard([[("✅", f"a:{day}:{pid}"), ("❌", f"r:{day}:{pid}"), ("🔄 Заменить", f"g:{day}:{pid}")]])


def is_admin(ctx: Context, user_id) -> bool:
    return bool(ctx.secrets.admin_chat_id) and str(user_id) == str(ctx.secrets.admin_chat_id)


def process_updates(ctx: Context) -> None:
    meta = ctx.state.meta()
    try:
        updates = ctx.bot.updates(meta["update_offset"])
    except TelegramError as e:
        log.error("getUpdates: %s", e)
        return
    for upd in updates:
        meta["update_offset"] = upd["update_id"] + 1
        ctx.state.save_meta(meta)  # сохраняем сразу, чтобы не обработать дважды при сбое
        try:
            if "callback_query" in upd:
                handle_callback(ctx, upd["callback_query"])
            elif "message" in upd:
                handle_message(ctx, upd["message"])
            elif "channel_post" in upd:
                handle_channel_post(ctx, upd["channel_post"])
        except Exception:
            log.exception("обработка update %s", upd.get("update_id"))
        meta = ctx.state.meta()


# --- кнопки ---
def handle_callback(ctx: Context, cb: dict) -> None:
    if not is_admin(ctx, cb["from"]["id"]):
        ctx.bot.answer(cb["id"], "Нет доступа")
        return
    data = cb.get("data", "")
    parts = data.split(":")
    action = parts[0]
    msg = cb.get("message", {})
    chat_id, message_id = msg.get("chat", {}).get("id"), msg.get("message_id")
    if action == "x" or len(parts) < 2:
        ctx.bot.answer(cb["id"])
        return
    day = parts[1]
    queue = ctx.state.load_queue(date.fromisoformat(day))
    if not queue:
        ctx.bot.answer(cb["id"], "План не найден")
        return

    if action == "A":
        n = 0
        for p in queue["posts"]:
            if p["status"] == "pending":
                p["status"] = "approved"
                n += 1
        ctx.state.save_queue(queue)
        ctx.bot.answer(cb["id"], f"Утверждено: {n}")
        ctx.bot.edit_buttons(chat_id, message_id,
                             keyboard([[("✅ Всё утверждено", "x"), ("👀 Посмотреть", f"V:{day}")]]))
        return

    if action == "V":
        ctx.bot.answer(cb["id"], "Присылаю посты")
        send_previews(ctx, queue)
        ctx.state.save_queue(queue)
        return

    if len(parts) < 3:
        ctx.bot.answer(cb["id"])
        return
    pid = parts[2]
    post = next((p for p in queue["posts"] if p["id"] == pid), None)
    if not post:
        ctx.bot.answer(cb["id"], "Пост не найден")
        return
    if post["status"] in ("published", "failed", "expired"):
        ctx.bot.answer(cb["id"], "Пост уже не изменить")
        return

    if action == "a":
        post["status"] = "approved"
        ctx.bot.answer(cb["id"], "Утверждён")
        ctx.bot.edit_buttons(chat_id, message_id, keyboard([[("✅ Утверждён", "x"), ("❌", f"r:{day}:{pid}")]]))
    elif action == "r":
        post["status"] = "rejected"
        ctx.bot.answer(cb["id"], "Не выйдет")
        ctx.bot.edit_buttons(chat_id, message_id, keyboard([[("❌ Отклонён", "x"), ("↩️ Вернуть", f"a:{day}:{pid}")]]))
    elif action == "g":
        ctx.bot.answer(cb["id"], "Готовлю замену, около минуты")
        ctx.bot.edit_buttons(chat_id, message_id, keyboard([[("🔄 Заменяю…", "x")]]))
        try:
            new = rebuild_post(ctx, post)
        except Exception as e:
            log.exception("замена поста")
            new = None
            ctx.bot.send_text(ctx.secrets.admin_chat_id, f"⚠️ Не удалось заменить пост: {esc(str(e)[:200])}")
        if new:
            idx = queue["posts"].index(post)
            queue["posts"][idx] = new
            ctx.bot.edit_buttons(chat_id, message_id, keyboard([[("🔄 Заменён ниже", "x")]]))
            send_preview(ctx, queue, new)
        else:
            ctx.bot.edit_buttons(chat_id, message_id, post_buttons(day, pid))
    ctx.state.save_queue(queue)


def preview_header(ctx: Context, post: dict) -> str:
    ch = ctx.channel(post["channel"])
    t = datetime.fromisoformat(post["time"])
    title = esc(ch.title) if ch else post["channel"]
    hand = "" if post.get("auto", True) else " · ✋ ручное"
    return f"<i>{ch.emoji if ch else ''} {title} · {t:%H:%M} · {esc(post['rubric'])}{hand}</i>\n\n"


def send_preview(ctx: Context, queue: dict, post: dict) -> None:
    admin = ctx.secrets.admin_chat_id
    header = preview_header(ctx, post)
    buttons = post_buttons(queue["date"], post["id"])
    try:
        if post["kind"] == "photo":
            # заголовок может не влезть в подпись — отправляем его отдельно
            ctx.bot.send_text(admin, header.strip())
            msg = ctx.bot.send_photo(admin, post["media"], post["text"], buttons)
        elif post["kind"] == "photo_text":
            ctx.bot.send_text(admin, header.strip())
            ctx.bot.send_photo(admin, post["media"])
            msg = ctx.bot.send_text(admin, post["text"], buttons)
        elif post["kind"] == "video":
            ctx.bot.send_text(admin, header.strip())
            msg = ctx.bot.send_video(admin, post["media"], post["text"], buttons)
        else:
            msg = ctx.bot.send_text(admin, header + post["text"], buttons)
        post["preview_message_id"] = msg["message_id"]
    except TelegramError as e:
        ctx.bot.send_text(admin, f"{header}⚠️ Предпросмотр не отправился: {esc(str(e)[:200])}", buttons)


def send_previews(ctx: Context, queue: dict) -> None:
    for post in queue["posts"]:
        if post["status"] in ("pending", "approved"):
            send_preview(ctx, queue, post)


# --- команды ---
def handle_channel_post(ctx: Context, post: dict) -> None:
    """Команда /id в канале, где бот админ: бот отвечает id канала (нужно для настройки)."""
    if (post.get("text") or "").strip().split("@")[0] == "/id":
        chat = post["chat"]
        ctx.bot.send_text(chat["id"], f"id этого канала: <code>{chat['id']}</code>\n"
                                      "Скопируйте его в настройки и удалите эти сообщения.")



def handle_message(ctx: Context, msg: dict) -> None:
    text = (msg.get("text") or "").strip()
    chat_id = msg["chat"]["id"]
    user_id = msg.get("from", {}).get("id")
    if not is_admin(ctx, user_id):
        if text.startswith("/start") and not ctx.secrets.admin_chat_id:
            ctx.bot.send_text(chat_id, f"Ваш chat id: <code>{user_id}</code>\n"
                                       "Добавьте его в секрет ADMIN_CHAT_ID.")
        return
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    arg = arg.strip()
    if cmd in ("/start", "/help"):
        ctx.bot.send_text(chat_id, HELP)
    elif cmd == "/status":
        ctx.bot.send_text(chat_id, status_text(ctx))
    elif cmd == "/channels":
        ctx.bot.send_text(chat_id, channels_text(ctx))
    elif cmd in ("/pause", "/resume"):
        ctx.bot.send_text(chat_id, set_pause(ctx, arg, cmd == "/pause"))
    elif cmd == "/preview":
        ctx.bot.send_text(chat_id, preview_sample(ctx, arg))


def set_pause(ctx: Context, key: str, pause: bool) -> str:
    if not key:
        return "Укажите ключ канала или all, например: /pause auto"
    if key != "all" and not ctx.channel(key):
        return f"Нет канала «{esc(key)}». Список: /channels"
    meta = ctx.state.meta()
    paused = set(meta["paused"])
    if pause:
        paused.add(key)
    else:
        paused.discard(key)
    meta["paused"] = sorted(paused)
    ctx.state.save_meta(meta)
    return f"{'⏸ Пауза' if pause else '▶️ Возобновлено'}: {esc(key)}"


def status_text(ctx: Context) -> str:
    today = now().date()
    lines = []
    for q in ctx.state.active_queues(today):
        d = date.fromisoformat(q["date"])
        if d < today:
            continue
        counts: dict[str, int] = {}
        for p in q["posts"]:
            counts[p["status"]] = counts.get(p["status"], 0) + 1
        summary = " ".join(f"{STATUS_ICON.get(k, k)}{v}" for k, v in sorted(counts.items())) or "пусто"
        lines.append(f"<b>{short_date(d)}</b>: {summary}")
    paused = ctx.state.meta()["paused"]
    if paused:
        lines.append(f"⏸ На паузе: {esc(', '.join(paused))}")
    return "\n".join(lines) or "Планов пока нет."


def channels_text(ctx: Context) -> str:
    paused = set(ctx.state.meta()["paused"])
    lines = ["<b>Каналы</b> (ключ — название — статус)"]
    for c in ctx.channels:
        if c.key in paused or "all" in paused:
            st = "⏸ пауза"
        elif c.ready:
            st = "✅ работает"
        elif c.enabled:
            st = "⚠️ включён, но нет chat_id"
        else:
            st = "выключен"
        lines.append(f"<code>{c.key}</code> — {c.emoji} {esc(c.title)} — {st}")
    return "\n".join(lines)


def preview_sample(ctx: Context, arg: str) -> str:
    from .core import build_text_post, fallback_text_rubric
    key, _, model = arg.partition(" ")
    model = model.strip() or None
    ch = ctx.channel(key.strip())
    if not ch:
        return "Укажите ключ канала: /preview auto (список: /channels)"
    rubric = fallback_text_rubric(ch, ctx.rng)
    if not rubric:
        return "У канала нет текстовых рубрик."
    history = ctx.state.history(ch.key)
    history_copy = {k: list(v) for k, v in history.items()}  # пробный пост не попадает в историю
    used_model = model or ctx.ai.model_for(ch)
    try:
        post = build_text_post(ctx, ch, rubric, now().date(), history_copy, model=model)
    except Exception as e:
        return f"⚠️ Модель {esc(used_model)} не справилась: {esc(str(e)[:300])}"
    admin = ctx.secrets.admin_chat_id
    pic = {"art": " · картинка YandexART", "pixabay": " · картинка Pixabay", "pexels": " · картинка Pexels",
           "openverse": " · картинка Openverse"}.get(post.get("image_from"), " · ⚠️ без картинки")
    head = f"<i>Пробный пост · {esc(ch.title)} · {esc(rubric.name)} · {esc(used_model)}{pic}</i>"
    if post["kind"] == "photo":
        ctx.bot.send_text(admin, head)
        ctx.bot.send_photo(admin, post["media"], post["text"])
        return "☝️ Пробный пост выше, в канал он не уйдёт."
    if post["kind"] == "photo_text":
        ctx.bot.send_text(admin, head)
        ctx.bot.send_photo(admin, post["media"])
        ctx.bot.send_text(admin, post["text"])
        return "☝️ Пробный пост выше (фото и текст двумя сообщениями — текст длинный)."
    return f"{head}\n\n{post['text']}"
