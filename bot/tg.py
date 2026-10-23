"""Тонкая обёртка над Telegram Bot API."""
from __future__ import annotations

import json
import logging
import time

import requests

log = logging.getLogger(__name__)


class TelegramError(Exception):
    pass


def keyboard(rows: list[list[tuple[str, str]]]) -> dict:
    """[[("✅", "a:..."), ...], ...] -> inline_keyboard"""
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


class Bot:
    def __init__(self, token: str, dry_run: bool = False):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.dry_run = dry_run
        self._fake_id = 1000

    def call(self, method: str, files: dict | None = None, **params):
        params = {k: v for k, v in params.items() if v is not None}
        if self.dry_run:
            log.info("DRY %s %s", method, {k: str(v)[:120] for k, v in params.items()})
            return self._fake(method)
        data = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
                for k, v in params.items()}
        for attempt in range(3):
            resp = requests.post(self.base + method, data=data, files=files, timeout=180)
            body = resp.json()
            if body.get("ok"):
                return body["result"]
            retry = body.get("parameters", {}).get("retry_after")
            if retry and attempt < 2:
                time.sleep(min(int(retry), 30))
                continue
            raise TelegramError(f"{method}: {body.get('description')}")
        raise TelegramError(f"{method}: не удалось после повторов")

    def _fake(self, method: str):
        self._fake_id += 1
        if method == "getUpdates":
            return []
        if method == "getChatMemberCount":
            return 0
        if method in ("answerCallbackQuery", "editMessageReplyMarkup", "editMessageText"):
            return True
        return {"message_id": self._fake_id,
                "photo": [{"file_id": f"photo{self._fake_id}"}],
                "video": {"file_id": f"video{self._fake_id}"}}

    # --- удобные методы ---
    def send_text(self, chat_id, text, buttons=None, preview=False):
        return self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                         reply_markup=buttons, disable_web_page_preview=not preview)

    def send_photo(self, chat_id, photo, caption="", buttons=None):
        """photo — file_id/URL или байты картинки."""
        if isinstance(photo, (bytes, bytearray)):
            return self.call("sendPhoto", files={"photo": ("image.jpg", bytes(photo), "image/jpeg")},
                             chat_id=chat_id, caption=caption or None, parse_mode="HTML",
                             reply_markup=buttons)
        return self.call("sendPhoto", chat_id=chat_id, photo=photo, caption=caption or None,
                         parse_mode="HTML", reply_markup=buttons)

    def send_video(self, chat_id, video, caption="", buttons=None):
        """video — file_id/URL или путь к локальному файлу."""
        if isinstance(video, str) and (video.startswith("/") or video.startswith(".")):
            with open(video, "rb") as fh:
                return self.call("sendVideo", files={"video": fh}, chat_id=chat_id,
                                 caption=caption or None, parse_mode="HTML",
                                 supports_streaming=True, reply_markup=buttons)
        return self.call("sendVideo", chat_id=chat_id, video=video, caption=caption or None,
                         parse_mode="HTML", supports_streaming=True, reply_markup=buttons)

    def edit_buttons(self, chat_id, message_id, buttons=None):
        try:
            return self.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                             reply_markup=buttons or {"inline_keyboard": []})
        except TelegramError as e:  # сообщение не изменилось / удалено — не страшно
            log.warning("edit_buttons: %s", e)

    def edit_text(self, chat_id, message_id, text, buttons=None):
        try:
            return self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                             parse_mode="HTML", reply_markup=buttons, disable_web_page_preview=True)
        except TelegramError as e:
            log.warning("edit_text: %s", e)

    def answer(self, callback_id, text=""):
        try:
            return self.call("answerCallbackQuery", callback_query_id=callback_id, text=text or None)
        except TelegramError as e:  # кнопку нажали давно — ответ уже не нужен
            log.warning("answer: %s", e)

    def updates(self, offset: int):
        return self.call("getUpdates", offset=offset, timeout=0,
                         allowed_updates=["message", "callback_query", "channel_post"])

    def member_count(self, chat_id) -> int:
        return self.call("getChatMemberCount", chat_id=chat_id)


def file_id_of(message: dict, kind: str) -> str | None:
    if kind == "photo" and message.get("photo"):
        return message["photo"][-1]["file_id"]
    if kind == "video":
        for key in ("video", "animation", "document"):
            if message.get(key):
                return message[key]["file_id"]
    return None
