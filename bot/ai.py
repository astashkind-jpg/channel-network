"""Генерация текстов через Yandex AI Studio (OpenAI-совместимый API).

Доступны модели Яндекса и сторонние модели из AI Studio — DeepSeek, Qwen, Alice AI и др.
Модель задаётся в config/network.yaml (default_model) и при желании для каждого канала (model).
"""
from __future__ import annotations

import json
import logging
import re
import time

import requests

from .settings import Channel, Network, Rubric, Secrets
from .util import extract_json

log = logging.getLogger(__name__)

YANDEX_CHAT_URL = "https://llm.api.cloud.yandex.net/v1/chat/completions"

BASE_RULES = """Общие правила для всех постов:
- Пиши по-русски, живо и по делу, как автор канала, а не как энциклопедия.
- Объём текста — до 700 знаков. Короткие абзацы, 1–3 эмодзи по смыслу.
- Без хештегов, без markdown (никаких *, #, _), без призывов ставить реакции и подписываться.
- Не выдумывай точные цифры, даты, цитаты и названия, если не уверен в них. Лучше общее, но верное.
- Не упоминай, что текст написан ИИ."""

THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)


class AIError(Exception):
    pass


def model_uri(model: str, folder_id: str) -> str:
    """'deepseek-v4.1-flash' -> 'gpt://<folder>/deepseek-v4.1-flash'; полный URI оставляем как есть."""
    model = model.strip()
    if model.startswith("gpt://"):
        return model
    return f"gpt://{folder_id}/{model}"


class AI:
    def __init__(self, secrets: Secrets, network: Network, dry_run: bool = False):
        self.s = secrets
        self.n = network
        self.dry_run = dry_run

    def model_for(self, ch: Channel) -> str:
        return ch.model or self.n.default_model

    # --- низкоуровневый вызов ---
    def complete(self, model: str, system: str, user: str, temperature: float = 0.8) -> str:
        if self.dry_run:
            return json.dumps({"topic": "тестовая тема", "text": "Тестовый пост (DRY_RUN).",
                               "image_query": "car engine"}, ensure_ascii=False)
        last_err = None
        for attempt in range(3):
            try:
                return self._chat(model, system, user, temperature)
            except AIError as e:
                last_err = e
                if " 400" in str(e) or " 401" in str(e) or " 403" in str(e) or " 404" in str(e):
                    break  # ошибка настроек — повтор не поможет
                log.warning("AI %s попытка %d: %s", model, attempt + 1, e)
            except (requests.RequestException, KeyError, IndexError, ValueError) as e:
                last_err = e
                log.warning("AI %s попытка %d: %s", model, attempt + 1, e)
            time.sleep(3 * (attempt + 1))
        raise AIError(f"{model}: {last_err}")

    def _chat(self, model: str, system: str, user: str, temperature: float) -> str:
        if not (self.s.yandex_api_key and self.s.yandex_folder_id):
            raise AIError("не заданы YANDEX_API_KEY / YANDEX_FOLDER_ID")
        resp = requests.post(
            YANDEX_CHAT_URL,
            headers={"Authorization": f"Bearer {self.s.yandex_api_key}",
                     "OpenAI-Project": self.s.yandex_folder_id,
                     "Content-Type": "application/json"},
            json={"model": model_uri(model, self.s.yandex_folder_id),
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user}],
                  "temperature": temperature,
                  "max_tokens": 2000},
            timeout=180,
        )
        if resp.status_code != 200:
            raise AIError(f"Yandex HTTP {resp.status_code}: {resp.text[:300]}")
        content = resp.json()["choices"][0]["message"].get("content") or ""
        # рассуждающие модели иногда возвращают ход мыслей в <think>…</think>
        content = THINK_RE.sub("", content).strip()
        if not content:
            raise AIError("модель вернула пустой ответ (возможно, отказ по теме)")
        return content

    # --- посты ---
    def generate_post(self, ch: Channel, rubric: Rubric, day_text: str, recent: list[str],
                      model: str | None = None) -> dict:
        system = f"{ch.persona.strip()}\n\n{BASE_RULES}"
        avoid = "; ".join(recent[-40:]) or "—"
        user = (
            f"Канал: «{ch.title}». Рубрика: «{rubric.name}». Пост выйдет: {day_text}.\n\n"
            f"Задание рубрики: {rubric.prompt.strip()}\n\n"
            f"Темы, которые уже были (не повторяй их): {avoid}\n\n"
            "Ответь строго одним JSON-объектом без пояснений:\n"
            '{"topic": "тема поста в 3–6 словах", "text": "готовый текст поста", '
            '"image_prompt": "описание фото к посту на русском, 15–30 слов: конкретная сцена из текста — '
            'что именно в кадре, крупный план главного предмета, обстановка, время года и суток; без людей '
            'крупным планом, без текста и логотипов", '
            '"image_query": "2–3 английских слова — главный предмет на фото буквально, '
            'например frozen windshield wiper"}'
        )
        raw = self.complete(model or self.model_for(ch), system, user)
        data = extract_json(raw)
        text = str(data.get("text", "")).strip()
        if len(text) < 40:
            raise AIError(f"слишком короткий текст: {raw[:200]}")
        return {"topic": str(data.get("topic", "")).strip()[:80] or text[:50],
                "text": text,
                "image_query": str(data.get("image_query", "")).strip(),
                "image_prompt": str(data.get("image_prompt", "")).strip()}

    def video_caption(self, ch: Channel, original: str) -> str:
        system = (f"{ch.persona.strip()}\n\nТы пишешь короткие подписи к видео в канале. "
                  "1–2 предложения, до 250 знаков, 1 эмодзи. Без хештегов и ссылок. "
                  "Не пересказывай рекламу и не упоминай чужие каналы.")
        user = ("Подпись автора оригинального видео (может быть пустой или на другом языке):\n"
                f"«{(original or '').strip()[:800]}»\n\n"
                "Напиши свою подпись к этому видео. Ответь только текстом подписи.")
        if self.dry_run:
            return "Смотрите, что творится 👀"
        text = self.complete(self.model_for(ch), system, user, temperature=0.9).strip().strip("«»\"")
        return text[:400]
