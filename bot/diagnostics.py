"""Команда /check — самопроверка бота: Yandex AI Studio, фотостоки, Telegram-каналы."""
from __future__ import annotations

import logging
import time

from . import images
from .core import Context
from .tg import TelegramError
from .util import esc

log = logging.getLogger(__name__)

OK, FAIL, WARN, OFF = "✅", "❌", "⚠️", "➖"


def _timed(fn):
    t = time.time()
    result = fn()
    return result, time.time() - t


def _short(e: Exception) -> str:
    return esc(str(e).replace("\n", " ")[:220])


def check_text_models(ctx: Context) -> list[str]:
    """Каждая модель, которая используется в каналах, отвечает на короткий запрос."""
    lines = []
    models = {ctx.network.default_model} | {c.model for c in ctx.channels if c.model}
    for model in sorted(models):
        try:
            answer, sec = _timed(lambda: ctx.ai.complete(
                model, "Ты проверка связи.", "Ответь одним словом по-русски: работает", temperature=0))
            lines.append(f"{OK} Тексты <code>{esc(model)}</code>: «{esc(answer.strip()[:30])}», {sec:.1f} с")
        except Exception as e:
            lines.append(f"{FAIL} Тексты <code>{esc(model)}</code>: {_short(e)}")
    return lines


def check_vision(ctx: Context) -> list[str]:
    """Модель со зрением, которая выбирает картинки: показываем ей одно фото с Pixabay."""
    model = ctx.network.image_check_model
    if not ctx.network.image_check:
        return [f"{OFF} Проверка картинок ИИ выключена (image_check: false)"]
    cands = images.search_pixabay("car engine", ctx.secrets.pixabay_key)[:1] or \
        images.search_wikimedia("car engine")[:1]
    if not cands:
        return [f"{WARN} Проверка картинок ИИ <code>{esc(model)}</code>: не нашлось фото для теста"]
    try:
        n, sec = _timed(lambda: ctx.ai.pick_image("двигатель автомобиля",
                                                   "Пост про подкапотное пространство автомобиля.", cands))
        verdict = "фото подходит" if n == 0 else "фото не подходит"
        return [f"{OK} Проверка картинок ИИ <code>{esc(model)}</code>: работает ({verdict}), {sec:.1f} с"]
    except Exception as e:
        return [f"{FAIL} Проверка картинок ИИ <code>{esc(model)}</code>: {_short(e)}\n"
                f"    Бот продолжит работать: фото будут выбираться только по тегам."]


def check_gigachat(ctx: Context) -> list[str]:
    uses = ctx.network.image_source == "gigachat" or any(c.image_source == "gigachat" for c in ctx.channels)
    if not uses:
        return [f"{OFF} GigaChat выключен (image_source не gigachat)"]
    if not ctx.secrets.gigachat_key:
        return [f"{WARN} GigaChat: нет секрета GIGACHAT_AUTH_KEY — картинки берутся с фотостоков"]
    cert = images.giga_ca()
    if cert is True:
        return [f"{FAIL} GigaChat: нет сертификата Минцифры (certs/russian_trusted_root_ca.pem)"]
    try:
        images.giga_token(ctx.secrets.gigachat_key)
    except Exception as e:
        return [f"{FAIL} GigaChat авторизация: {_short(e)} — проверьте GIGACHAT_AUTH_KEY"]
    data, sec = _timed(lambda: images.generate_gigachat(
        "автомобиль вечером на парковке, у него горит только одна передняя фара", ctx.secrets.gigachat_key))
    if data:
        return [f"{OK} GigaChat рисует картинки ({len(data) // 1024} КБ, {sec:.0f} с)"]
    return [f"{FAIL} GigaChat: авторизация есть, но картинка не нарисовалась — см. лог Actions "
            f"(возможно, генерация не входит в ваш пакет)"]


def check_art(ctx: Context) -> list[str]:
    uses = ctx.network.image_source == "generate" or any(c.image_source == "generate" for c in ctx.channels)
    if not uses:
        return [f"{OFF} YandexART выключен (картинки — бесплатные фотостоки)"]
    data = images.generate_image("Расширительный бачок с антифризом под капотом автомобиля",
                                 ctx.secrets.yandex_api_key, ctx.secrets.yandex_folder_id,
                                 ctx.network.art_model, ctx.network.image_aspect)
    if data:
        return [f"{OK} YandexART: картинка сгенерирована ({len(data) // 1024} КБ)"]
    return [f"{FAIL} YandexART: не сработал — нужна роль ai.imageGeneration.user и ключ с этой областью"]


def check_stocks(ctx: Context) -> list[str]:
    lines = []
    tests = [
        ("Pixabay", lambda: images.search_pixabay("coolant reservoir", ctx.secrets.pixabay_key),
         bool(ctx.secrets.pixabay_key)),
        ("Pexels", lambda: images.search_pexels("coolant reservoir", ctx.secrets.pexels_key),
         bool(ctx.secrets.pexels_key)),
        ("Wikimedia Commons", lambda: images.search_wikimedia("coolant reservoir"), True),
        ("Openverse", lambda: images.search_openverse("coolant reservoir"), True),
    ]
    for name, fn, enabled in tests:
        if not enabled:
            lines.append(f"{OFF} {name}: нет ключа — источник пропускается")
            continue
        try:
            found, sec = _timed(fn)
        except Exception as e:
            lines.append(f"{WARN} {name}: {_short(e)}")
            continue
        matched = sum(1 for c in found if c.score > 0)
        if found:
            lines.append(f"{OK} {name}: найдено {len(found)}, по теме {matched}, {sec:.1f} с")
        else:
            lines.append(f"{WARN} {name}: ничего не найдено или источник недоступен")
    return lines


def check_telegram(ctx: Context) -> list[str]:
    lines = []
    try:
        me = ctx.bot.call("getMe")
        bot_id = me["id"]
        lines.append(f"{OK} Бот @{esc(me.get('username', ''))} на связи")
    except (TelegramError, KeyError) as e:
        return [f"{FAIL} Бот: {_short(e)} — проверьте TG_BOT_TOKEN"]

    def rights(chat_id, title):
        try:
            m = ctx.bot.call("getChatMember", chat_id=chat_id, user_id=bot_id)
        except TelegramError as e:
            hint = ""
            if "not found" in str(e).lower():
                hint = (f"\n    Сейчас в настройках: <code>{esc(str(chat_id))}</code>. Нужен @адрес публичного канала "
                        "или числовой id вида -100… (напишите /id в канале).")
            return f"{FAIL} {title}: {_short(e)}{hint}"
        if m.get("status") == "administrator" and m.get("can_post_messages", True):
            return f"{OK} {title}: бот — админ, может публиковать"
        if m.get("status") == "administrator":
            return f"{FAIL} {title}: бот — админ, но без права «Публикация сообщений»"
        return f"{FAIL} {title}: бот не администратор"

    if ctx.secrets.storage_chat_id:
        lines.append(rights(ctx.secrets.storage_chat_id, "Склад медиа"))
    else:
        lines.append(f"{WARN} Склад медиа: STORAGE_CHAT_ID не задан")
    for ch in ctx.channels:
        if ch.ready:
            lines.append(rights(ch.chat_id, f"{ch.emoji} {esc(ch.title)}"))
    waiting = [c.title for c in ctx.channels if c.enabled and not c.chat_id]
    if waiting:
        lines.append(f"{OFF} Ждут подключения ({len(waiting)}): {esc(', '.join(waiting))}")
    return lines


def run_checks(ctx: Context) -> str:
    sections = [
        ("🤖 Telegram", check_telegram),
        ("🧠 Yandex AI Studio", lambda c: check_text_models(c) + check_vision(c) + check_art(c)),
        ("🎨 GigaChat", check_gigachat),
        ("🖼 Фотостоки", check_stocks),
    ]
    out = ["🩺 <b>Проверка бота</b>"]
    fails = 0
    for title, fn in sections:
        try:
            lines = fn(ctx)
        except Exception as e:  # проверка не должна падать целиком
            log.exception("проверка %s", title)
            lines = [f"{FAIL} {_short(e)}"]
        fails += sum(1 for line in lines if line.startswith(FAIL))
        out += ["", f"<b>{title}</b>"] + lines
    out += ["", f"{OK} Всё работает." if not fails else f"{FAIL} Проблем: {fails}. Решения — в README, раздел «Если что-то не работает»."]
    return "\n".join(out)
