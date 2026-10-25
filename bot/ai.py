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
                if "пустой ответ" in str(e) and attempt >= 1:
                    break  # дважды пусто — пусть пишет запасная модель
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
                  "max_tokens": 6000},  # рассуждающим моделям нужен запас на «размышления»
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
            '"image_prompt": "задание художнику на русском, 30–60 слов (правила ниже)", '
            '"image_queries": ["4 запроса для поиска фото на фотостоке, по-английски, 2–4 слова"]}\n\n'
            "Как составлять image_prompt — по нему нейросеть нарисует АТМОСФЕРНУЮ ИЛЛЮСТРАЦИЮ к посту. "
            "Картинка передаёт тему и настроение, а технические детали остаются в тексте. Нейросеть хорошо рисует "
            "знакомые предметы целиком и общим или средним планом (машина, дверь, капот, колесо, салон, дорога, "
            "кухня, блюдо, сад, животное, погода, время года) и плохо — мелкие детали крупным планом (отверстия, "
            "нити, гранулы, разъёмы, надписи, схемы). Поэтому: "
            "1) выбери сцену, по которой сразу понятна тема поста, без мелких деталей (например, пост про "
            "дренажные отверстия двери → «нижняя часть двери легкового автомобиля под осенним дождём, капли стекают "
            "по кузову, мокрые листья на асфальте»; пост про обогрев заднего стекла → «задняя часть автомобиля "
            "морозным утром, заднее стекло покрыто инеем»); "
            "2) укажи время года, погоду, время суток и общий ракурс; "
            "3) не добавляй предметы, которых нет в посте, и людей крупным планом; без текста, надписей и логотипов. "
            "30–50 слов.\n\n"
            "Как составлять image_queries. Первый запрос — ГЛАВНАЯ СЦЕНА поста, то, что должно быть на фото, "
            "так, как фотограф подписал бы снимок. Следующие три — та же сцена другими словами (синонимы, "
            "другой ракурс), от точного к чуть более общему. Не называй второстепенные предметы из советов "
            "(коврик, лопату, шину, тряпку), если пост не о них. Без общих слов вроде car, auto, food, nature "
            "без уточнения. Примеры: пост «как выбраться, если машина застряла в снегу» → "
            '["car stuck in snow", "car stuck in mud", "wheel spinning in snow", "snowdrift car"]; '
            'пост про уровень антифриза → ["coolant reservoir", "engine coolant level", "radiator cap", '
            '"car engine bay"]; рецепт сырников → ["syrniki", "cottage cheese pancakes", "pancakes plate", '
            '"breakfast pancakes"].'
        )
        first = model or self.model_for(ch)
        chain = [first] + [m for m in self.n.fallback_models if m and m != first]
        errors = []
        data, text, used = {}, "", first
        for used in chain:
            try:
                raw = self.complete(used, system, user)
                data = extract_json(raw)
                text = str(data.get("text", "")).strip()
                if len(text) < 40:
                    raise AIError(f"слишком короткий текст: {raw[:120]}")
                break
            except (AIError, ValueError) as e:
                errors.append(f"{used}: {e}")
                log.warning("пост моделью %s не получился, пробую следующую: %s", used, e)
                text = ""
        if not text:
            raise AIError("; ".join(errors)[:400])
        queries = data.get("image_queries") or []
        if isinstance(queries, str):
            queries = [queries]
        queries = [str(q).strip() for q in queries if str(q).strip() and not str(q).startswith("три запроса")]
        if data.get("image_query"):
            queries.insert(0, str(data["image_query"]).strip())
        return {"topic": str(data.get("topic", "")).strip()[:80] or text[:50],
                "text": text,
                "image_queries": queries[:4],
                "image_query": queries[0] if queries else "",
                "image_prompt": str(data.get("image_prompt", "")).strip(),
                "model": used}

    def pick_image(self, topic: str, text: str, candidates: list) -> int | None:
        """ИИ со зрением смотрит на найденные фото и выбирает то, где виден предмет поста.
        Возвращает индекс фото, None — если ни одно не подходит."""
        import base64
        if self.dry_run or not candidates:
            return 0
        parts = [{"type": "text", "text": (
            f"Пост в Telegram-канале. Тема: «{topic}». Текст: «{text[:500]}»\n\n"
            f"Ниже {len(candidates)} фото, пронумерованы по порядку с 1. Какое лучше всего иллюстрирует "
            "ГЛАВНУЮ СЦЕНУ этого поста — то, о чём пост, а не предмет, упомянутый вскользь? Например, к посту "
            "о машине, застрявшей в снегу, подходит фото застрявшей машины, а стопка шин или коврик — нет. "
            "Хорошее качество и отсутствие надписей/водяных знаков — плюс. Ответь только числом — номером фото. "
            "Если ни одно не показывает главную сцену поста, ответь 0.")}]
        shown = 0
        for c in candidates:
            try:
                r = requests.get(c.preview or c.url, timeout=20,
                                 headers={"User-Agent": "channel-network-bot/1.0"})
                r.raise_for_status()
                if len(r.content) > 3_000_000:
                    raise ValueError("слишком большой файл")
                b64 = base64.b64encode(r.content).decode()
                mime = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
            except (requests.RequestException, ValueError):
                b64, mime = "", ""
            if not b64:  # фото не скачалось — показываем заглушку, чтобы номера не съехали
                parts.append({"type": "text", "text": f"Фото {len(parts)}: (не загрузилось)"})
                continue
            parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
            shown += 1
        if not shown:
            raise AIError("ни одно фото не скачалось для проверки")
        resp = requests.post(
            YANDEX_CHAT_URL,
            headers={"Authorization": f"Bearer {self.s.yandex_api_key}",
                     "OpenAI-Project": self.s.yandex_folder_id, "Content-Type": "application/json"},
            json={"model": model_uri(self.n.image_check_model, self.s.yandex_folder_id),
                  "messages": [{"role": "user", "content": parts}], "temperature": 0,
                  # рассуждающие модели (Qwen) сначала «думают» — нужен запас токенов
                  "max_tokens": 2000},
            timeout=120)
        if resp.status_code != 200:
            raise AIError(f"проверка картинок HTTP {resp.status_code}: {resp.text[:200]}")
        message = resp.json()["choices"][0]["message"]
        answer = THINK_RE.sub("", message.get("content") or "").strip()
        if not answer:  # ответ мог остаться только в рассуждениях — берём последнее число оттуда
            reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
            nums = re.findall(r"\b\d+\b", reasoning)
            answer = nums[-1] if nums else ""
        m = re.search(r"\d+", answer)
        if not m:
            raise AIError(f"непонятный ответ модели: «{answer[:50] or 'пусто'}»")
        n = int(m.group())
        if n == 0:
            return None
        if 1 <= n <= len(candidates):
            return n - 1
        raise AIError(f"номер вне диапазона: {n}")

    def check_generated(self, topic: str, text: str, image_prompt: str, data: bytes) -> tuple[bool, str]:
        """ИИ со зрением сравнивает нарисованную картинку с постом.
        Возвращает (подходит, что исправить)."""
        import base64
        if self.dry_run:
            return True, ""
        parts = [
            {"type": "text", "text": (
                f"Пост в Telegram-канале. Тема: «{topic}». Текст: «{text[:600]}»\n"
                f"Художнику заказали: «{image_prompt[:400]}»\n\n"
                "Посмотри на картинку. Это атмосферная иллюстрация: она НЕ обязана показывать мелкие технические "
                "детали, но по ней должна быть понятна тема поста. Подходит, если: сцена про то же, о чём пост "
                "(тот же предмет общим планом, то же время года и погода); нет предметов, которые сбивают смысл "
                "(например, ключи и телефон к посту о влаге в салоне); нет текста, надписей и уродливых искажений.\n"
                "Ответь в одну строку: «ДА» — если подходит; или «НЕТ: <что исправить, коротко, как указание "
                "художнику>».")},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(data).decode()}},
        ]
        resp = requests.post(
            YANDEX_CHAT_URL,
            headers={"Authorization": f"Bearer {self.s.yandex_api_key}",
                     "OpenAI-Project": self.s.yandex_folder_id, "Content-Type": "application/json"},
            json={"model": model_uri(self.n.image_check_model, self.s.yandex_folder_id),
                  "messages": [{"role": "user", "content": parts}], "temperature": 0, "max_tokens": 2000},
            timeout=120)
        if resp.status_code != 200:
            raise AIError(f"проверка картинки HTTP {resp.status_code}: {resp.text[:200]}")
        message = resp.json()["choices"][0]["message"]
        answer = THINK_RE.sub("", message.get("content") or "").strip()
        if not answer:
            answer = (message.get("reasoning_content") or "").strip().splitlines()[-1:] or [""]
            answer = answer[0]
        low = answer.lower().lstrip("«\"' ")
        if low.startswith("да"):
            return True, ""
        if low.startswith("нет"):
            return False, (answer.split(":", 1)[1].strip(" «»\"'.")[:300] if ":" in answer else "")
        raise AIError(f"непонятный ответ проверки: «{answer[:60]}»")

    def shorten(self, ch: Channel, text: str, limit: int, model: str | None = None) -> str:
        """Сокращает текст поста, чтобы он поместился в подпись к фото."""
        if self.dry_run:
            return text[:limit]
        system = f"{ch.persona.strip()}\n\nТы редактор. Сокращаешь пост, сохраняя смысл, тон, эмодзи и структуру."
        user = (f"Сократи пост до {limit - 80} знаков максимум. Ответь только новым текстом поста, "
                f"без пояснений.\n\n{text}")
        return self.complete(model or self.model_for(ch), system, user, temperature=0.4).strip()

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
