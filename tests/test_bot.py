"""Сквозные тесты без сети: Telegram, ИИ, Pixabay и репост подменены заглушками.

Запуск: python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import random
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from bot import approvals, core, images as _images, planner, publisher, report

REAL_FIND = _images.find_candidates  # настоящая функция — до подмены в тестах
from bot.ai import AI
from bot.settings import Secrets, load_channels, load_network
from bot.state import State
from bot.tg import Bot
from bot.util import TZ, extract_json, parse_window

ADMIN = "111"


class FakeBot(Bot):
    def __init__(self):
        super().__init__("TEST")
        self.calls: list[tuple[str, dict]] = []
        self.pending_updates: list[dict] = []
        self.fail_chats: set[str] = set()
        self._mid = 0

    def call(self, method, files=None, **params):
        self.calls.append((method, params))
        if params.get("chat_id") in self.fail_chats and method.startswith("send"):
            from bot.tg import TelegramError
            raise TelegramError("chat not found")
        self._mid += 1
        if method == "getUpdates":
            ups, self.pending_updates = self.pending_updates, []
            return ups
        if method == "getChatMemberCount":
            return 500 + self._mid
        if method in ("answerCallbackQuery", "editMessageReplyMarkup", "editMessageText"):
            return True
        return {"message_id": self._mid, "photo": [{"file_id": f"ph{self._mid}"}],
                "video": {"file_id": f"vid{self._mid}"}}

    def published(self):
        """Отправки в сами каналы сети (не админу и не в служебный канал)."""
        return [(m, p) for m, p in self.calls
                if m.startswith("send") and str(p.get("chat_id", "")).endswith("_chan")]

    def sent_to(self, chat_id, method=None):
        return [p for m, p in self.calls if p.get("chat_id") == chat_id and (method is None or m == method)]


class FakeAI(AI):
    def __init__(self):
        super().__init__(Secrets(), load_network())
        self.n_calls = 0

    def complete(self, model, system, user, temperature=0.8):
        self.n_calls += 1
        self.last_model = model
        return ("Вот пост:\n```json\n" + json.dumps({
            "topic": f"тема {self.n_calls}",
            "text": f"Текст поста номер {self.n_calls} <важно> & полезно. " * 3,
            "image_queries": ["test photo", "photo"], "image_prompt": "тестовая сцена"}, ensure_ascii=False) + "\n```")

    def video_caption(self, ch, original):
        return "Подпись к видео"


def make_ctx(tmp: Path, seed: int = 1) -> core.Context:
    channels = load_channels()
    for c in channels:
        c.chat_id = f"@{c.key}_chan"
        c.invite_link = f"https://t.me/+{c.key}"
    bot = FakeBot()
    secrets = Secrets(bot_token="T", admin_chat_id=ADMIN, storage_chat_id="-100999",
                      pixabay_key="k", tg_api_id="1", tg_api_hash="h", tg_session="s")
    network = load_network()
    network.image_check = False  # в большинстве тестов ИИ-проверка картинок не нужна
    return core.Context(network=network, channels=channels, secrets=secrets, bot=bot,
                        ai=FakeAI(), state=State(tmp), rng=random.Random(seed))


def fake_videos(sources, used, count, secrets, network, dry_run=False):
    out = []
    for i in range(count):
        f = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        f.write(b"x")
        f.close()
        out.append({"source": "@src", "key": f"@src:{len(used) + i + random.randint(0, 10**6)}",
                    "text": "orig", "path": f.name, "views": 100})
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ctx = make_ctx(self.tmp)
        from bot.images import Candidate
        self.p_img = mock.patch("bot.images.find_candidates", side_effect=lambda *a, **k: [
            Candidate(f"pixabay:{random.random()}", "https://img/x.jpg", 2, via="tags")])
        self.p_vid = mock.patch("bot.reposter.find_videos", side_effect=fake_videos)
        self.p_art = mock.patch("bot.images.generate_image", return_value=b"\xff\xd8jpeg")
        self.p_dl = mock.patch("bot.core.download_image", return_value=None)
        self.p_dl.start()
        self.p_stock = mock.patch("bot.stockvideo.find_stock_videos", return_value=[])
        self.p_stock.start()
        self.p_img.start()
        self.p_vid.start()
        self.p_art.start()
        # у каналов с video-рубриками должны быть источники, иначе видео не ищется вовсе
        for c in self.ctx.channels:
            if any(r.type == "video" for r in c.rubrics):
                c.video_sources = ["@src"]

    def tearDown(self):
        self.p_stock.stop()
        self.p_img.stop()
        self.p_vid.stop()
        self.p_art.stop()
        self.p_dl.stop()

    def plan(self, day=date(2026, 10, 24)):  # суббота
        return planner.plan_day(self.ctx, day)


class TestConfig(unittest.TestCase):
    def test_channels_valid(self):
        channels = load_channels()
        self.assertEqual(len(channels), 8)
        for c in channels:
            self.assertTrue(c.windows, c.key)
            for w in c.windows:
                parse_window(w)
            self.assertIn(c.cluster, ("main", "adult"))
            self.assertTrue(c.rubrics)
            for r in c.rubrics:
                self.assertIn(r.type, ("text", "video"))
                for s in r.slots or []:
                    self.assertLess(s, len(c.windows), f"{c.key}/{r.name}")
        adult = {c.key for c in channels if c.cluster == "adult"}
        self.assertEqual(adult, {"intimacy", "afterdark"})
        self.assertFalse(next(c for c in channels if c.key == "afterdark").auto_approve)

    def test_extract_json(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})
        self.assertEqual(extract_json('Ответ: {"a": "x}y", "b": {"c": 2}} спасибо'),
                         {"a": "x}y", "b": {"c": 2}})
        self.assertEqual(extract_json("```json\n{\"a\": 2}\n```"), {"a": 2})
        with self.assertRaises(ValueError):
            extract_json("нет json")


class TestPlan(Base):
    def test_plan_builds_all_channels(self):
        q = self.plan()
        expected = sum(len(c.windows) for c in self.ctx.channels)
        self.assertEqual(len(q["posts"]), expected, q["errors"])
        self.assertEqual(q["errors"], [])
        kinds = {p["kind"] for p in q["posts"]}
        self.assertTrue({"photo", "video"} <= kinds)
        # HTML экранирован
        text_post = next(p for p in q["posts"] if p["kind"] == "photo")
        self.assertIn("&lt;важно&gt; &amp;", text_post["text"])
        # видео — со ссылкой на источник и с file_id из служебного канала
        v = next(p for p in q["posts"] if p["kind"] == "video")
        self.assertIn("Источник: @src", v["text"])
        self.assertTrue(v["media"].startswith("vid"))
        # анекдоты 18+ без картинок, ручное утверждение
        for p in q["posts"]:
            if p["channel"] == "afterdark":
                self.assertFalse(p["auto"])
                if p["rubric"] == "Анекдот для взрослых":
                    self.assertEqual(p["kind"], "photo")  # картинки теперь у всех постов

    def test_cards_rubrics_bound_to_windows(self):
        q = self.plan()
        cards = sorted((p for p in q["posts"] if p["channel"] == "cards"), key=lambda p: p["time"])
        self.assertEqual([p["rubric"] for p in cards], ["Доброе утро", "Повод дня", "Спокойной ночи"])

    def test_times_inside_windows_and_sorted(self):
        q = self.plan()
        times = [p["time"] for p in q["posts"]]
        self.assertEqual(times, sorted(times))
        for p in q["posts"]:
            ch = self.ctx.channel(p["channel"])
            t = datetime.fromisoformat(p["time"]).timetz().replace(tzinfo=None)
            self.assertTrue(any(parse_window(w)[0] <= t <= parse_window(w)[1] for w in ch.windows),
                            (p["id"], t))

    def test_promo_only_inside_cluster(self):
        q = self.plan()
        for p in q["posts"]:
            if "👉" not in p["text"]:
                continue
            ch = self.ctx.channel(p["channel"])
            linked = [c for c in self.ctx.channels if f"https://t.me/+{c.key}" in p["text"]]
            self.assertEqual(len(linked), 1)
            self.assertEqual(linked[0].cluster, ch.cluster, p["id"])
            self.assertNotEqual(linked[0].key, ch.key)
        promo_channels = {p["channel"] for p in q["posts"] if "👉" in p["text"]}
        self.assertEqual(promo_channels, {c.key for c in self.ctx.channels})

    def test_digest_sent(self):
        q = self.plan()
        digest = self.ctx.bot.sent_to(ADMIN, "sendMessage")[-1]
        self.assertIn("План на 24 октября, суббота", digest["text"])
        self.assertIn("Выйдут только после вашего ✅: После полуночи", digest["text"])
        buttons = digest["reply_markup"]["inline_keyboard"][0]
        self.assertEqual(buttons[0]["callback_data"], "A:2026-10-24")
        self.assertEqual(q["digest_message_id"] is not None, True)

    def test_plan_is_idempotent(self):
        q1 = self.plan()
        n_ai = self.ctx.ai.n_calls
        q2 = self.plan()
        self.assertEqual(q1["created"], q2["created"])
        self.assertEqual(self.ctx.ai.n_calls, n_ai)

    def test_sunday_catalog(self):
        q = self.plan(date(2026, 10, 25))
        cat = [p for p in q["posts"] if p["rubric"] == "Каталог сети"]
        self.assertEqual(len(cat), 8)
        main_cat = next(p for p in cat if p["channel"] == "auto")
        self.assertNotIn("intimacy", main_cat["text"])
        self.assertIn("t.me/+diy", main_cat["text"])
        adult_cat = next(p for p in cat if p["channel"] == "intimacy")
        self.assertIn("t.me/+afterdark", adult_cat["text"])
        self.assertNotIn("t.me/+auto", adult_cat["text"])

    def test_channel_without_chat_id_skipped_and_ai_error_reported(self):
        self.ctx.channel("food").chat_id = ""
        orig = self.ctx.ai.generate_post

        def broken(ch, rubric, day, recent, model=None):
            if ch.key == "history":
                raise RuntimeError("Yandex HTTP 500")
            return orig(ch, rubric, day, recent, model=model)
        self.ctx.ai.generate_post = broken
        q = self.plan()
        chans = {p["channel"] for p in q["posts"]}
        self.assertNotIn("food", chans)
        self.assertNotIn("history", chans)
        self.assertTrue(any("Было время" in e for e in q["errors"]))
        digest = self.ctx.bot.sent_to(ADMIN, "sendMessage")[-1]["text"]
        self.assertIn("Не получилось", digest)

    def test_no_video_falls_back_to_text(self):
        self.p_vid.stop()
        with mock.patch("bot.reposter.find_videos", return_value=[]):
            q = self.plan()
        self.p_vid.start()
        self.assertFalse(any(p["kind"] == "video" for p in q["posts"]))
        nature = [p for p in q["posts"] if p["channel"] == "nature"]
        self.assertEqual(len(nature), 3)

    def test_topics_saved_to_history(self):
        self.plan()
        h = self.ctx.state.history("auto")
        self.assertTrue(h["topics"])
        self.assertTrue(h["members"])


class TestApprovalsAndPublishing(Base):
    def cb(self, data, user=ADMIN):
        return {"update_id": random.randint(1, 10**9),
                "callback_query": {"id": "c1", "from": {"id": int(user)}, "data": data,
                                   "message": {"chat": {"id": int(ADMIN)}, "message_id": 5}}}

    def run_updates(self, *updates):
        self.ctx.bot.pending_updates = list(updates)
        approvals.process_updates(self.ctx)

    def shift_times(self, q, minutes_ago=5):
        past = (datetime.now(TZ) - timedelta(minutes=minutes_ago)).isoformat()
        for p in q["posts"]:
            p["time"] = past
        self.ctx.state.save_queue(q)

    def test_approve_all_then_publish_once(self):
        day = date.today() + timedelta(days=1)
        q = self.plan(day)
        self.run_updates(self.cb(f"A:{day}"))
        q = self.ctx.state.load_queue(day)
        self.assertTrue(all(p["status"] == "approved" for p in q["posts"]))
        self.shift_times(q)
        with mock.patch("bot.publisher.now", return_value=datetime.now(TZ)):
            # tick смотрит очереди вчера/сегодня/завтра — завтрашняя тоже подходит
            publisher.tick(self.ctx)
            n_published = len(self.ctx.bot.published())
            publisher.tick(self.ctx)  # повторный тик не дублирует
        n_again = len(self.ctx.bot.published())
        self.assertEqual(n_published, len(q["posts"]))
        self.assertEqual(n_again, n_published)
        q = self.ctx.state.load_queue(day)
        self.assertTrue(all(p["status"] == "published" for p in q["posts"]))

    def test_auto_approve_after_deadline_except_manual(self):
        day = date.today() + timedelta(days=1)
        q = self.plan(day)
        q["deadline"] = (datetime.now(TZ) - timedelta(minutes=1)).isoformat()
        self.ctx.state.save_queue(q)
        publisher.tick(self.ctx)
        q = self.ctx.state.load_queue(day)
        for p in q["posts"]:
            if p["channel"] == "afterdark" and p["rubric"] != "Каталог сети":  # каталог — всегда авто
                self.assertEqual(p["status"], "pending")
            else:
                self.assertEqual(p["status"], "approved", p["id"])

    def test_before_deadline_nothing_published(self):
        day = date.today() + timedelta(days=1)
        q = self.plan(day)
        self.shift_times(q)
        publisher.tick(self.ctx)
        self.assertEqual(self.ctx.bot.published(), [])

    def test_reject_and_restore_single(self):
        day = date.today() + timedelta(days=1)
        q = self.plan(day)
        pid = q["posts"][0]["id"]
        self.run_updates(self.cb(f"r:{day}:{pid}"))
        self.assertEqual(self.ctx.state.load_queue(day)["posts"][0]["status"], "rejected")
        self.run_updates(self.cb(f"A:{day}"))  # «утвердить всё» не воскрешает отклонённый
        self.assertEqual(self.ctx.state.load_queue(day)["posts"][0]["status"], "rejected")
        self.run_updates(self.cb(f"a:{day}:{pid}"))
        self.assertEqual(self.ctx.state.load_queue(day)["posts"][0]["status"], "approved")

    def test_regenerate_replaces_post(self):
        day = date.today() + timedelta(days=1)
        q = self.plan(day)
        post = next(p for p in q["posts"] if p["kind"] == "photo")
        old_text = post["text"]
        self.run_updates(self.cb(f"g:{day}:{post['id']}"))
        q = self.ctx.state.load_queue(day)
        new = next(p for p in q["posts"] if p["id"] == post["id"])
        self.assertNotEqual(new["text"], old_text)
        self.assertEqual(new["time"], post["time"])
        self.assertEqual(new["status"], "pending")
        self.assertTrue(self.ctx.bot.sent_to(ADMIN, "sendPhoto"))

    def test_view_sends_previews_with_buttons(self):
        day = date.today() + timedelta(days=1)
        q = self.plan(day)
        before = len(self.ctx.bot.calls)
        self.run_updates(self.cb(f"V:{day}"))
        previews = [p for m, p in self.ctx.bot.calls[before:]
                    if p.get("chat_id") == ADMIN and p.get("reply_markup") and m.startswith("send")]
        self.assertEqual(len(previews), len(q["posts"]))

    def test_non_admin_cannot_approve(self):
        day = date.today() + timedelta(days=1)
        self.plan(day)
        self.run_updates(self.cb(f"A:{day}", user="999"))
        q = self.ctx.state.load_queue(day)
        self.assertTrue(all(p["status"] == "pending" for p in q["posts"]))

    def test_manual_post_expires(self):
        day = date.today() + timedelta(days=1)
        q = self.plan(day)
        q["deadline"] = (datetime.now(TZ) - timedelta(hours=20)).isoformat()
        self.shift_times(q, minutes_ago=13 * 60)
        publisher.tick(self.ctx)
        q = self.ctx.state.load_queue(day)
        for p in q["posts"]:
            manual = p["channel"] == "afterdark" and p["rubric"] != "Каталог сети"
            # 13 ч опоздания: ручной сгорает без ✅, автоматический — как опоздавший больше 3 ч
            self.assertEqual(p["status"], "expired")
            if not manual:
                self.assertIn("опоздал", p.get("error", ""))

    def test_pause_blocks_publishing(self):
        day = date.today() + timedelta(days=1)
        q = self.plan(day)
        msg = {"update_id": 1, "message": {"text": "/pause auto", "chat": {"id": int(ADMIN)},
                                           "from": {"id": int(ADMIN)}}}
        self.run_updates(msg, self.cb(f"A:{day}"))
        self.shift_times(self.ctx.state.load_queue(day))
        publisher.tick(self.ctx)
        q = self.ctx.state.load_queue(day)
        for p in q["posts"]:
            self.assertEqual(p["status"], "approved" if p["channel"] == "auto" else "published")

    def test_publish_error_reported(self):
        day = date.today() + timedelta(days=1)
        self.plan(day)
        self.ctx.bot.fail_chats.add("@diy_chan")
        self.run_updates(self.cb(f"A:{day}"))
        self.shift_times(self.ctx.state.load_queue(day))
        publisher.tick(self.ctx)
        q = self.ctx.state.load_queue(day)
        self.assertTrue(all(p["status"] == "failed" for p in q["posts"] if p["channel"] == "diy"))
        alert = self.ctx.bot.sent_to(ADMIN, "sendMessage")[-1]["text"]
        self.assertIn("Не опубликовалось", alert)

    def test_commands(self):
        def say(text, user=ADMIN):
            self.run_updates({"update_id": 1, "message": {"text": text, "chat": {"id": int(user)},
                                                          "from": {"id": int(user)}}})
            sent = self.ctx.bot.sent_to(int(user), "sendMessage") or self.ctx.bot.sent_to(user, "sendMessage")
            return sent[-1]["text"] if sent else None
        self.assertIn("Команды", say("/start"))
        self.assertIn("auto", say("/channels"))
        self.plan(date.today() + timedelta(days=1))
        self.assertIn("⏳", say("/status"))
        self.assertIn("Пауза", say("/pause all"))
        self.assertEqual(self.ctx.ready_channels(), [])
        self.assertIn("Возобновлено", say("/resume all"))
        self.assertIsNone(say("/start", user="555"))  # чужой при заданном ADMIN_CHAT_ID — тишина
        self.ctx.secrets.admin_chat_id = ""
        self.assertIn("555", say("/start", user="555"))


class TestModels(Base):
    def test_default_and_channel_model(self):
        from bot.ai import model_uri
        self.assertEqual(model_uri("deepseek-v4.1-flash", "b1g"), "gpt://b1g/deepseek-v4.1-flash")
        self.assertEqual(model_uri("gpt://b1g/x/latest", "zzz"), "gpt://b1g/x/latest")
        ch = self.ctx.channel("auto")
        self.assertEqual(self.ctx.ai.model_for(ch), self.ctx.network.default_model)
        ch.model = "qwen3.6-35b-a3b"
        self.assertEqual(self.ctx.ai.model_for(ch), "qwen3.6-35b-a3b")

    def test_preview_with_model(self):
        self.ctx.bot.pending_updates = [{"update_id": 3, "message": {
            "text": "/preview food aliceai-llm", "chat": {"id": int(ADMIN)}, "from": {"id": int(ADMIN)}}}]
        approvals.process_updates(self.ctx)
        self.assertEqual(self.ctx.ai.last_model, "aliceai-llm")
        texts = [p.get("text", "") for p in self.ctx.bot.sent_to(int(ADMIN)) + self.ctx.bot.sent_to(ADMIN)]
        self.assertTrue(any("aliceai-llm" in t for t in texts))

    def test_think_tags_stripped_and_request_format(self):
        from bot.ai import AI
        ai = AI(Secrets(yandex_api_key="KEY", yandex_folder_id="b1g"), load_network())
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"choices": [{"message": {"content": "<think>хм</think>Ответ"}}]}
        with mock.patch("bot.ai.requests.post", return_value=resp) as post:
            self.assertEqual(ai.complete("deepseek-v4.1-flash", "s", "u"), "Ответ")
        kw = post.call_args.kwargs
        self.assertEqual(post.call_args.args[0], "https://llm.api.cloud.yandex.net/v1/chat/completions")
        self.assertEqual(kw["headers"]["Authorization"], "Bearer KEY")
        self.assertEqual(kw["headers"]["OpenAI-Project"], "b1g")
        self.assertEqual(kw["json"]["model"], "gpt://b1g/deepseek-v4.1-flash")

    def test_bad_model_no_retry(self):
        from bot.ai import AI, AIError
        ai = AI(Secrets(yandex_api_key="KEY", yandex_folder_id="b1g"), load_network())
        resp = mock.Mock(status_code=404, text="model not found")
        with mock.patch("bot.ai.requests.post", return_value=resp) as post, \
                mock.patch("bot.ai.time.sleep"):
            with self.assertRaises(AIError):
                ai.complete("no-such-model", "s", "u")
        self.assertEqual(post.call_count, 1)


class TestImages(Base):
    def test_generated_image_used_first(self):
        self.ctx.network.image_source = "generate"
        q = self.plan()
        photos = [p for p in q["posts"] if p["kind"] == "photo"]
        self.assertTrue(photos)
        self.assertTrue(all(p.get("image_from") == "art" for p in photos))
        # байты картинки ушли в служебный канал файлом
        uploads = [p for m, p in self.ctx.bot.calls if m == "sendPhoto" and p.get("chat_id") == "-100999"]
        self.assertTrue(uploads)

    def test_fallback_to_pixabay_when_generation_fails(self):
        self.ctx.network.image_source = "generate"
        self.p_art.stop()
        with mock.patch("bot.images.generate_image", return_value=None):
            q = self.plan()
        self.p_art.start()
        photos = [p for p in q["posts"] if p["kind"] == "photo"]
        self.assertTrue(photos)
        self.assertTrue(all(p.get("image_from") == "pixabay" for p in photos))

    def test_pixabay_only_channel(self):
        self.ctx.network.image_source = "generate"
        self.ctx.channel("food").image_source = "free"
        with mock.patch("bot.images.generate_image", return_value=b"x") as gen:
            q = self.plan()
        food = [p for p in q["posts"] if p["channel"] == "food" and p["kind"] == "photo"]
        self.assertTrue(all(p["image_from"] == "pixabay" for p in food))

    def test_pixabay_scoring_prefers_matching_tags(self):
        self.p_img.stop()
        from bot import images
        resp = mock.Mock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {"hits": [
            {"id": 1, "tags": "soap, foam, sponge", "largeImageURL": "u1", "webformatURL": "w1"},
            {"id": 2, "tags": "car, windshield, wipers, frost", "largeImageURL": "u2", "webformatURL": "w2"},
            {"id": 3, "tags": "car, road", "largeImageURL": "u3", "webformatURL": "w3"}]}
        with mock.patch("bot.images.requests.get", return_value=resp):
            got = images.find_image("frozen windshield wiper", "k", [], random.Random(1))
        self.p_img.start()
        self.assertEqual(got, ("2", "u2"))

    def test_pixabay_no_matching_tags_returns_none(self):
        self.p_img.stop()
        from bot import images
        resp = mock.Mock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {"hits": [{"id": 1, "tags": "soap, foam", "largeImageURL": "u1", "webformatURL": "w1"}]}
        with mock.patch("bot.images.requests.get", return_value=resp):
            got = images.find_image("windshield wiper", "k", [], random.Random(1))
        self.p_img.start()
        self.assertIsNone(got)

    def test_generate_image_polls_operation(self):
        from bot import images
        import base64
        post = mock.Mock(status_code=200)
        post.json.return_value = {"id": "op1"}
        pending = mock.Mock(); pending.json.return_value = {"done": False}
        done = mock.Mock(); done.json.return_value = {"done": True, "response": {"image": base64.b64encode(b"IMG").decode()}}
        self.p_art.stop()
        with mock.patch("bot.images.requests.post", return_value=post) as rp, \
                mock.patch("bot.images.requests.get", side_effect=[pending, done]), \
                mock.patch("bot.images.time.sleep"):
            data = images.generate_image("дворник примёрз к стеклу", "KEY", "b1g")
        self.p_art.start()
        self.assertEqual(data, b"IMG")
        body = rp.call_args.kwargs["json"]
        self.assertEqual(body["modelUri"], "art://b1g/yandex-art/latest")
        self.assertIn("дворник", body["messages"][0]["text"])
        self.assertEqual(rp.call_args.kwargs["headers"]["Authorization"], "Api-Key KEY")


class TestFreeImages(Base):
    def _resp(self, payload):
        r = mock.Mock()
        r.raise_for_status = lambda: None
        r.json.return_value = payload
        return r

    def _by_url(self, mapping):
        def fake_get(url, **kw):
            for part, payload in mapping.items():
                if part in url:
                    return self._resp(payload)
            return self._resp({})
        return fake_get

    def test_best_match_across_sources(self):
        mapping = {
            "pixabay": {"hits": [{"id": 1, "tags": "car, road", "webformatURL": "p1"}]},
            "pexels": {"photos": [{"id": 7, "alt": "Engine coolant reservoir under the hood",
                                   "src": {"large2x": "x7", "large": "x7s", "medium": "x7m"}}]},
            "wikimedia": {"query": {"pages": {}}},
            "openverse": {"results": []},
        }
        with mock.patch("bot.images.requests.get", side_effect=self._by_url(mapping)):
            got = REAL_FIND(["coolant reservoir"], "car", "pk", "xk", [], random.Random(1))
        self.assertEqual(got[0].key, "pexels:7")
        self.assertEqual(got[0].credit, "📷 Pexels")
        self.assertEqual(got[0].preview, "x7m")

    def test_wikimedia_license_filter_and_credit(self):
        from bot import images
        pages = {"query": {"pages": {
            "1": {"pageid": 1, "index": 1, "title": "File:Coolant reservoir.jpg", "imageinfo": [{
                "mime": "image/jpeg", "width": 2000, "thumburl": "t1", "url": "u1",
                "extmetadata": {"LicenseShortName": {"value": "CC BY-SA 4.0"},
                                "Artist": {"value": "<a href='x'>Ivan Petrov</a>"}}}]},
            "2": {"pageid": 2, "index": 2, "title": "File:Coolant tank.jpg", "imageinfo": [{
                "mime": "image/jpeg", "width": 2000, "thumburl": "t2", "url": "u2",
                "extmetadata": {"LicenseShortName": {"value": "CC BY-NC-SA 2.0"}}}]},
            "3": {"pageid": 3, "index": 3, "title": "File:Radiator coolant.jpg", "imageinfo": [{
                "mime": "image/jpeg", "width": 2000, "thumburl": "t3", "url": "u3",
                "extmetadata": {"LicenseShortName": {"value": "Public domain"}}}]},
        }}}
        with mock.patch("bot.images.requests.get", return_value=self._resp(pages)):
            got = images.search_wikimedia("coolant reservoir")
        keys = [c.key for c in got]
        self.assertEqual(keys, ["wikimedia:1", "wikimedia:3"])  # NC отброшена
        self.assertEqual(got[0].credit, "📷 Ivan Petrov, CC BY-SA 4.0 / Wikimedia Commons")
        self.assertEqual(got[1].credit, "")  # общественное достояние — без подписи

    def test_judge_picks_and_rejects(self):
        from bot.images import Candidate
        mapping = {"pixabay": {"hits": [
            {"id": 1, "tags": "coolant, reservoir", "webformatURL": "a"},
            {"id": 2, "tags": "coolant, reservoir, engine", "webformatURL": "b"}]}}
        with mock.patch("bot.images.requests.get", side_effect=self._by_url(mapping)):
            got = REAL_FIND(["coolant reservoir"], "car", "pk", "", [], random.Random(1),
                            judge=lambda cands: [c.key for c in cands].index("pixabay:2"))
        self.assertEqual(got[0].key, "pixabay:2")
        mapping["pixabay"]["hits"].append({"id": 3, "tags": "car", "webformatURL": "c"})
        with mock.patch("bot.images.requests.get", side_effect=self._by_url(mapping)):
            got = REAL_FIND(["coolant reservoir"], "car", "pk", "", [], random.Random(1),
                            judge=lambda cands: None)  # ИИ: ни одно не подходит
        self.assertEqual(got[0].key, "pixabay:3")  # тематическое фото канала
        with mock.patch("bot.images.requests.get", side_effect=self._by_url(mapping)):
            got = REAL_FIND(["coolant reservoir"], "car", "pk", "", [], random.Random(1),
                            judge=lambda cands: (_ for _ in ()).throw(RuntimeError("API")))
        self.assertIn(got[0].key, ("pixabay:1", "pixabay:2"))  # ошибка проверки — берём по тегам

    def test_pick_image_request(self):
        from bot.ai import AI
        from bot.images import Candidate
        ai = AI(Secrets(yandex_api_key="K", yandex_folder_id="f"), load_network())
        img = mock.Mock(content=b"jpg", headers={"Content-Type": "image/jpeg"})
        img.raise_for_status = lambda: None
        answer = mock.Mock(status_code=200)
        answer.json.return_value = {"choices": [{"message": {"content": "2"}}]}
        with mock.patch("bot.ai.requests.get", return_value=img), \
                mock.patch("bot.ai.requests.post", return_value=answer) as post:
            n = ai.pick_image("антифриз", "текст", [Candidate("a", "u1", 1), Candidate("b", "u2", 1)])
        self.assertEqual(n, 1)
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["model"], "gpt://f/qwen3.6-35b-a3b")
        kinds = [p["type"] for p in body["messages"][0]["content"]]
        self.assertEqual(kinds, ["text", "image_url", "image_url"])
        answer.json.return_value = {"choices": [{"message": {"content": "0"}}]}
        with mock.patch("bot.ai.requests.get", return_value=img), \
                mock.patch("bot.ai.requests.post", return_value=answer):
            self.assertIsNone(ai.pick_image("t", "x", [Candidate("a", "u1", 1)]))

    def test_fallback_query_guarantees_image(self):
        from bot import images
        def fake_get(url, **kw):
            q = kw.get("params", {}).get("q") or kw.get("params", {}).get("query")
            if q == "car":
                return self._resp({"hits": [{"id": 9, "tags": "auto, vehicle", "webformatURL": "c9"}]})
            return self._resp({"hits": [], "photos": [], "results": []})
        with mock.patch("bot.images.requests.get", side_effect=fake_get):
            got = REAL_FIND(["very rare thing"], "car", "pk", "", [], random.Random(1))
        self.assertEqual([c.key for c in got], ["pixabay:9"])

    def test_used_images_not_repeated(self):
        from bot import images
        resp = self._resp({"hits": [{"id": 1, "tags": "car", "webformatURL": "a"},
                                    {"id": 2, "tags": "car", "webformatURL": "b"}]})
        with mock.patch("bot.images.requests.get", return_value=resp):
            got = REAL_FIND(["car"], "", "pk", "", ["pixabay:1"], random.Random(1))
        self.assertTrue(got and all(c.key != "pixabay:1" for c in got))

    def test_openverse_licenses(self):
        from bot import images
        resp = self._resp({"results": [
            {"id": "a", "url": "u", "title": "old car", "tags": [], "license": "by", "license_version": "2.0",
             "creator": "Anna"},
            {"id": "b", "url": "u2", "title": "old car", "tags": [], "license": "cc0"},
            {"id": "c", "url": "u3", "title": "old car", "tags": [], "license": "by-nd"}]})
        with mock.patch("bot.images.requests.get", return_value=resp) as g:
            got = images.search_openverse("old car")
        self.assertEqual(g.call_args.kwargs["params"]["license_type"], "commercial")
        self.assertEqual([c.key for c in got], ["openverse:a", "openverse:b"])
        self.assertEqual(got[0].credit, "📷 Anna, CC BY 2.0")
        self.assertEqual(got[1].credit, "")

    def test_every_post_has_image_and_pexels_credit(self):
        from bot.images import Candidate
        self.p_img.stop()
        with mock.patch("bot.images.find_candidates", side_effect=lambda *a, **k: [
                Candidate(f"pexels:{random.random()}", "https://img/p.jpg", 1, credit="📷 Pexels")]):
            q = self.plan()
        self.p_img.start()
        for p in q["posts"]:
            self.assertIn(p["kind"], ("photo", "photo_text", "video"), p["id"])
            if p["kind"] != "video":
                self.assertIn("📷 Pexels", p["text"])
        self.assertEqual([e for e in q["errors"] if "без картинки" in e], [])

    def test_unreachable_image_tries_next(self):
        from bot.images import Candidate
        self.p_img.stop()
        bot = self.ctx.bot
        orig = bot.call
        def call(method, files=None, **params):
            if method == "sendPhoto" and params.get("photo") == "https://bad":
                from bot.tg import TelegramError
                raise TelegramError("wrong file identifier/HTTP URL specified")
            return orig(method, files=files, **params)
        bot.call = call
        with mock.patch("bot.images.find_candidates", return_value=[
                Candidate("pixabay:1", "https://bad", 2), Candidate("pixabay:2", "https://good", 2)]):
            post = core.build_text_post(self.ctx, self.ctx.channel("auto"),
                                        self.ctx.channel("auto").rubrics[0], date(2026, 10, 24),
                                        self.ctx.state.history("auto"))
        self.p_img.start()
        self.assertEqual(post["kind"], "photo")

    def test_no_image_reported_in_digest(self):
        self.p_img.stop()
        with mock.patch("bot.images.find_candidates", return_value=[]):
            q = self.plan()
        self.p_img.start()
        self.assertTrue(any("без картинки" in e for e in q["errors"]))

    def test_long_text_shortened_or_split(self):
        ai = self.ctx.ai
        ai.generate_post = lambda ch, r, d, rec, model=None: {
            "topic": "t", "text": "Очень длинный текст. " * 80, "image_queries": ["car"], "image_prompt": ""}
        ai.shorten = lambda ch, text, limit, model=None: "Короткая версия поста, которая влезает в подпись."
        post = core.build_text_post(self.ctx, self.ctx.channel("auto"), self.ctx.channel("auto").rubrics[0],
                                    date(2026, 10, 24), self.ctx.state.history("auto"))
        self.assertEqual(post["kind"], "photo")
        self.assertIn("Короткая версия", post["text"])
        ai.shorten = lambda ch, text, limit, model=None: text  # ИИ не сократил
        post = core.build_text_post(self.ctx, self.ctx.channel("auto"), self.ctx.channel("auto").rubrics[0],
                                    date(2026, 10, 24), self.ctx.state.history("auto"))
        self.assertEqual(post["kind"], "photo_text")

    def test_photo_text_published_as_two_messages(self):
        self.ctx.channel("auto").chat_id = "@auto_chan"
        from bot import publisher
        before = len(self.ctx.bot.published())
        publisher.publish_post(self.ctx, {"channel": "auto", "kind": "photo_text", "media": "fid", "text": "t"})
        methods = [m for m, p in self.ctx.bot.published()[before:]]
        self.assertEqual(methods, ["sendPhoto", "sendMessage"])


class TestSceneImages(Base):
    def _resp(self, payload):
        r = mock.Mock()
        r.raise_for_status = lambda: None
        r.json.return_value = payload
        return r

    def _pixabay_by_query(self, table):
        def fake_get(url, **kw):
            if "pixabay" not in url:
                return self._resp({"query": {"pages": {}}, "results": [], "photos": []})
            q = kw["params"]["q"]
            return self._resp({"hits": [{"id": i, "tags": t, "webformatURL": f"w{i}"}
                                        for i, t in table.get(q, [])]})
        return fake_get

    def test_main_scene_beats_generic_object(self):
        """Регрессия: пост про застрявшую машину не должен получить стопку шин."""
        table = {
            "car stuck in snow": [(1, "car, snow, winter")],          # частичное совпадение (2 из 3)
            "tire": [(2, "tire, tires, rubber"), (3, "tire, wheel")],  # полное совпадение, но не та сцена
        }
        with mock.patch("bot.images.requests.get", side_effect=self._pixabay_by_query(table)):
            got = REAL_FIND(["car stuck in snow", "tire"], "car", "pk", "", [], random.Random(1))
        self.assertEqual(got[0].key, "pixabay:1")
        self.assertEqual(got[0].via, "tags")

    def test_judge_sees_main_scene_photos_and_second_batch(self):
        table = {"car stuck in snow": [(i, "car, stuck, snow") for i in range(1, 13)],
                 "wheel spinning snow": [(i, "wheel, snow") for i in range(20, 26)]}
        seen = []
        def judge(cands):
            seen.append([c.key for c in cands])
            if len(seen) == 1:
                return None          # первая подборка не подошла
            return 0
        with mock.patch("bot.images.requests.get", side_effect=self._pixabay_by_query(table)):
            got = REAL_FIND(["car stuck in snow", "wheel spinning snow"], "car", "pk", "", [],
                            random.Random(1), judge=judge)
        self.assertEqual(len(seen), 2)
        self.assertEqual(len(seen[0]), 8)
        main = sum(1 for k in seen[0] if int(k.split(":")[1]) < 20)
        self.assertGreaterEqual(main, 5)                       # в подборке больше всего — главная сцена
        self.assertTrue(set(seen[0]).isdisjoint(seen[1]))      # вторая подборка — другие фото
        self.assertEqual(got[0].via, "ai")
        self.assertEqual(got[0].key, seen[1][0])

    def test_judge_rejects_all_goes_to_fallback(self):
        table = {"car stuck in snow": [(1, "car, snow")], "car engine bay": [(9, "car, engine")]}
        with mock.patch("bot.images.requests.get", side_effect=self._pixabay_by_query(table)):
            got = REAL_FIND(["car stuck in snow"], "car engine bay", "pk", "", [], random.Random(1),
                            judge=lambda c: None)
        self.assertEqual(got[0].key, "pixabay:9")
        self.assertEqual(got[0].via, "fallback")

    def test_replace_image_button(self):
        from bot.images import Candidate
        day = date.today() + timedelta(days=1)
        q = self.plan(day)
        post = next(p for p in q["posts"] if p["kind"] == "photo")
        old_key = post["image_key"]
        self.p_img.stop()
        captured = {}
        def fake_find(queries, fb, pk, xk, used, rng, **kw):
            captured["used"] = used
            captured["queries"] = queries
            return [Candidate("wikimedia:77", "https://img/new.jpg", 2, credit="📷 Anna, CC BY 2.0", via="ai")]
        with mock.patch("bot.images.find_candidates", side_effect=fake_find):
            self.ctx.bot.pending_updates = [{"update_id": 9, "callback_query": {
                "id": "c", "from": {"id": int(ADMIN)}, "data": f"p:{day}:{post['id']}",
                "message": {"chat": {"id": int(ADMIN)}, "message_id": 5}}}]
            approvals.process_updates(self.ctx)
        self.p_img.start()
        new = next(p for p in self.ctx.state.load_queue(day)["posts"] if p["id"] == post["id"])
        self.assertIn(old_key, captured["used"])               # старая картинка исключена
        self.assertEqual(captured["queries"], post["image_queries"])
        self.assertEqual(new["image_key"], "wikimedia:77")
        self.assertTrue(new["text"].startswith(post["text"].split("\n\n📷")[0][:40]))  # текст тот же
        self.assertTrue(new["text"].endswith("📷 Anna, CC BY 2.0"))
        self.assertEqual(new["status"], "pending")
        sent = [p for m, p in self.ctx.bot.calls if m == "sendPhoto" and p.get("chat_id") == ADMIN]
        self.assertTrue(any("выбрана ИИ" in (p.get("caption") or "") for p in sent) or
                        any("выбрана ИИ" in (p.get("text") or "") for m, p in self.ctx.bot.calls))

    def test_preview_buttons_include_replace_image(self):
        from bot.approvals import post_buttons
        rows = post_buttons("2026-10-25", "auto-1")["inline_keyboard"]
        self.assertEqual(rows[1][0]["callback_data"], "p:2026-10-25:auto-1")
        self.assertEqual(len(post_buttons("d", "x", photo=False)["inline_keyboard"]), 1)


class TestGigaChat(Base):
    def test_generate_flow(self):
        from bot import images
        images._giga_token.clear()
        oauth = mock.Mock(status_code=200)
        oauth.json.return_value = {"access_token": "TOK", "expires_at": (time.time() + 1800) * 1000}
        chat = mock.Mock(status_code=200)
        chat.json.return_value = {"choices": [{"message": {
            "content": '<img src="a598ae3d-d3eb-454e-a8bf-0848193a603e" fuse="true"/>'}}]}
        file = mock.Mock(status_code=200, content=b"J" * 5000)
        with mock.patch("bot.images.requests.post", side_effect=[oauth, chat]) as post, \
                mock.patch("bot.images.requests.get", return_value=file) as get:
            data = images.generate_gigachat("машина вечером, горит одна фара", "AUTHKEY")
        self.assertEqual(data, b"J" * 5000)
        self.assertEqual(post.call_args_list[0].kwargs["headers"]["Authorization"], "Basic AUTHKEY")
        self.assertEqual(post.call_args_list[0].kwargs["data"], {"scope": "GIGACHAT_API_PERS"})
        body = post.call_args_list[1].kwargs["json"]
        self.assertEqual(body["function_call"], "auto")
        self.assertEqual(body["model"], images.GIGA_MODELS[0])
        self.assertIn("одна фара", body["messages"][1]["content"])
        self.assertTrue(get.call_args.args[0].endswith("/files/a598ae3d-d3eb-454e-a8bf-0848193a603e/content"))
        # токен кэшируется — второй вызов без авторизации
        with mock.patch("bot.images.requests.post", side_effect=[chat]) as post2, \
                mock.patch("bot.images.requests.get", return_value=file):
            images.generate_gigachat("x", "AUTHKEY")
        self.assertEqual(post2.call_count, 1)

    def test_no_image_in_answer(self):
        from bot import images
        images._giga_token.update(key="K", token="T", exp=time.time() + 1000)
        chat = mock.Mock(status_code=200)
        chat.json.return_value = {"choices": [{"message": {"content": "Не могу нарисовать"}}]}
        with mock.patch("bot.images.requests.post", return_value=chat) as post:
            self.assertIsNone(images.generate_gigachat("x", "K"))
        self.assertEqual(post.call_count, len(images.GIGA_MODELS))  # перепробовал все модели
        self.assertIn("ответил текстом", images.giga_last_error)

    def test_second_model_draws(self):
        from bot import images
        images._giga_token.update(key="K", token="T", exp=time.time() + 1000)
        text = mock.Mock(status_code=200)
        text.json.return_value = {"choices": [{"message": {"content": "Описание словами"}}]}
        pic = mock.Mock(status_code=200)
        pic.json.return_value = {"choices": [{"message": {"content": '<img src="id1" fuse="true"/>'}}]}
        file = mock.Mock(status_code=200, content=b"J" * 3000)
        with mock.patch("bot.images.requests.post", side_effect=[text, pic]) as post, \
                mock.patch("bot.images.requests.get", return_value=file):
            self.assertEqual(images.generate_gigachat("x", "K"), b"J" * 3000)
        self.assertEqual(post.call_args.kwargs["json"]["model"], images.GIGA_MODELS[1])
        self.assertEqual(images.giga_last_error, "")

    def test_payment_error_stops(self):
        from bot import images
        images._giga_token.update(key="K", token="T", exp=time.time() + 1000)
        r = mock.Mock(status_code=402, text="Payment Required")
        with mock.patch("bot.images.requests.post", return_value=r) as post:
            self.assertIsNone(images.generate_gigachat("x", "K"))
        self.assertEqual(post.call_count, 1)
        self.assertIn("402", images.giga_last_error)

    def test_attach_uses_gigachat_then_stocks(self):
        self.ctx.network.image_source = "gigachat"
        self.ctx.secrets.gigachat_key = "K"
        with mock.patch("bot.images.generate_gigachat", return_value=b"\xff\xd8img") as gen:
            post = core.build_text_post(self.ctx, self.ctx.channel("auto"), self.ctx.channel("auto").rubrics[0],
                                        date(2026, 10, 25), self.ctx.state.history("auto"))
        self.assertEqual(gen.call_args.args[0], "тестовая сцена")
        self.assertEqual(post["image_from"], "gigachat")
        self.assertEqual(post["image_via"], "gen")
        self.assertEqual(post["image_prompt"], "тестовая сцена")
        with mock.patch("bot.images.generate_gigachat", return_value=None):
            post = core.build_text_post(self.ctx, self.ctx.channel("auto"), self.ctx.channel("auto").rubrics[0],
                                        date(2026, 10, 25), self.ctx.state.history("auto"))
        self.assertEqual(post["image_via"], "tags")  # генерация не удалась — фото со стока

    def test_replace_image_regenerates(self):
        self.ctx.network.image_source = "gigachat"
        self.ctx.secrets.gigachat_key = "K"
        with mock.patch("bot.images.generate_gigachat", return_value=b"one"):
            post = core.build_text_post(self.ctx, self.ctx.channel("auto"), self.ctx.channel("auto").rubrics[0],
                                        date(2026, 10, 25), self.ctx.state.history("auto"))
        post.update({"id": "auto-1", "channel": "auto", "time": "2026-10-25T09:00:00+03:00",
                     "rubric": "Лайфхак", "auto": True, "status": "pending"})
        with mock.patch("bot.images.generate_gigachat", return_value=b"two") as gen:
            new = core.replace_image(self.ctx, post)
        self.assertEqual(gen.call_args.args[0], "тестовая сцена")
        self.assertEqual(new["image_via"], "gen")


class TestFallbackModels(unittest.TestCase):
    def test_empty_answer_switches_model(self):
        from bot.ai import AI, AIError
        ch = load_channels()[0]
        ai = AI(Secrets(yandex_api_key="K", yandex_folder_id="f"), load_network())
        calls = []
        def complete(model, system, user, temperature=0.8):
            calls.append(model)
            if model == "deepseek-v4.1-flash":
                raise AIError("модель вернула пустой ответ (возможно, отказ по теме)")
            return json.dumps({"topic": "t", "text": "Нормальный текст поста, достаточно длинный для проверки.",
                               "image_queries": ["car"], "image_prompt": "сцена"}, ensure_ascii=False)
        ai.complete = complete
        res = ai.generate_post(ch, ch.rubrics[0], "завтра", [])
        self.assertEqual(calls, ["deepseek-v4.1-flash", "qwen3.6-35b-a3b"])
        self.assertEqual(res["model"], "qwen3.6-35b-a3b")

    def test_all_models_fail(self):
        from bot.ai import AI, AIError
        ch = load_channels()[0]
        ai = AI(Secrets(), load_network())
        ai.complete = lambda *a, **k: (_ for _ in ()).throw(AIError("пусто"))
        with self.assertRaises(AIError) as cm:
            ai.generate_post(ch, ch.rubrics[0], "завтра", [])
        self.assertIn("aliceai-llm", str(cm.exception))

    def test_empty_retried_once_not_thrice(self):
        from bot.ai import AI, AIError
        ai = AI(Secrets(yandex_api_key="K", yandex_folder_id="f"), load_network())
        n = []
        def chat(*a):
            n.append(1)
            raise AIError("модель вернула пустой ответ")
        ai._chat = chat
        with mock.patch("bot.ai.time.sleep"), self.assertRaises(AIError):
            ai.complete("deepseek-v4.1-flash", "s", "u")
        self.assertEqual(len(n), 2)


class TestView(Base):
    def say(self, text):
        self.ctx.bot.pending_updates = [{"update_id": 1, "message": {
            "text": text, "chat": {"id": int(ADMIN)}, "from": {"id": int(ADMIN)}}}]
        approvals.process_updates(self.ctx)

    def texts(self):
        return [p.get("text") or "" for m, p in self.ctx.bot.calls if str(p.get("chat_id")) == ADMIN]

    def test_view_command_shows_tomorrow(self):
        day = date.today() + timedelta(days=1)
        q = self.plan(day)
        before = len(self.ctx.bot.calls)
        self.say("/view")
        new = self.ctx.bot.calls[before:]
        self.assertTrue(any("Посты на" in (p.get("text") or "") for m, p in new))
        with_buttons = [p for m, p in new if p.get("reply_markup") and m.startswith("send")]
        self.assertEqual(len(with_buttons), len(q["posts"]))

    def test_view_nothing_to_show_is_reported(self):
        day = date.today() + timedelta(days=1)
        q = self.plan(day)
        for p in q["posts"]:
            p["status"] = "published"
        self.ctx.state.save_queue(q)
        self.say(f"/view {day.isoformat()}")
        self.assertTrue(any("нечего показывать" in t and "📤" in t for t in self.texts()))

    def test_view_no_plan(self):
        self.say("/view")
        self.assertTrue(any("Плана пока нет" in t for t in self.texts()))

    def test_errors_are_reported_not_silent(self):
        day = date.today() + timedelta(days=1)
        self.plan(day)
        with mock.patch("bot.approvals.send_previews", side_effect=KeyError("rubric")):
            self.ctx.bot.pending_updates = [{"update_id": 3, "callback_query": {
                "id": "c", "from": {"id": int(ADMIN)}, "data": f"V:{day}",
                "message": {"chat": {"id": int(ADMIN)}, "message_id": 5}}}]
            approvals.process_updates(self.ctx)
        self.assertTrue(any("Ошибка при обработке кнопки" in t and "KeyError" in t for t in self.texts()))

    def test_button_on_missing_plan_reported(self):
        self.ctx.bot.pending_updates = [{"update_id": 4, "callback_query": {
            "id": "c", "from": {"id": int(ADMIN)}, "data": "V:2020-01-01",
            "message": {"chat": {"id": int(ADMIN)}, "message_id": 5}}}]
        approvals.process_updates(self.ctx)
        self.assertTrue(any("не найден" in t for t in self.texts()))


class TestGeneratedCheck(Base):
    def setUp(self):
        super().setUp()
        self.ctx.network.image_source = "gigachat"
        self.ctx.network.image_check = True
        self.ctx.secrets.gigachat_key = "K"
        self.ch = self.ctx.channel("auto")

    def build(self):
        return core.build_text_post(self.ctx, self.ch, self.ch.rubrics[0], date(2026, 10, 26),
                                    self.ctx.state.history("auto"))

    def test_redraw_with_fix_until_ok(self):
        verdicts = iter([(False, "показать заднее стекло с нитями обогрева"), (True, "")])
        self.ctx.ai.check_generated = lambda *a: next(verdicts)
        with mock.patch("bot.images.generate_gigachat", side_effect=[b"first", b"second"]) as gen:
            post = self.build()
        self.assertEqual(gen.call_count, 2)
        self.assertIn("Обязательно: показать заднее стекло", gen.call_args_list[1].args[0])
        self.assertEqual(post["image_via"], "gen")
        upload = [p for m, p in self.ctx.bot.calls if m == "sendPhoto" and p.get("chat_id") == "-100999"]
        self.assertTrue(upload)

    def test_both_rejected_stock_found_by_ai(self):
        from bot.images import Candidate
        self.ctx.ai.check_generated = lambda *a: (False, "нет мешочка")
        self.p_img.stop()
        with mock.patch("bot.images.generate_gigachat", return_value=b"img"), \
                mock.patch("bot.images.find_candidates",
                           return_value=[Candidate("wikimedia:5", "u", 2, via="ai")]):
            post = self.build()
        self.p_img.start()
        self.assertEqual(post["image_key"], "wikimedia:5")

    def test_both_rejected_no_good_stock_uses_generated_with_warning(self):
        from bot.images import Candidate
        self.ctx.ai.check_generated = lambda *a: (False, "нет мешочка")
        self.p_img.stop()
        with mock.patch("bot.images.generate_gigachat", return_value=b"img"), \
                mock.patch("bot.images.find_candidates",
                           return_value=[Candidate("pixabay:5", "u", 1, via="fallback")]):
            post = self.build()
        self.p_img.start()
        self.assertEqual(post["image_via"], "gen_warn")
        self.assertEqual(post["image_notes"][-1], "нет мешочка")
        from bot.approvals import preview_header
        post.update({"channel": "auto", "time": "2026-10-26T09:00:00+03:00", "rubric": "Лайфхак"})
        self.assertIn("Замечание ИИ к картинке: нет мешочка", preview_header(self.ctx, post))

    def test_check_failure_does_not_block(self):
        def boom(*a):
            raise RuntimeError("API down")
        self.ctx.ai.check_generated = boom
        with mock.patch("bot.images.generate_gigachat", return_value=b"img") as gen:
            post = self.build()
        self.assertEqual(gen.call_count, 1)
        self.assertEqual(post["image_via"], "gen")

    def test_check_generated_parsing(self):
        from bot.ai import AI
        ai = AI(Secrets(yandex_api_key="K", yandex_folder_id="f"), load_network())
        for content, expected in [("ДА", (True, "")), ("НЕТ: нарисуй заднее стекло", (False, "нарисуй заднее стекло")),
                                  ("«Нет: без ключей»", (False, "без ключей"))]:
            r = mock.Mock(status_code=200)
            r.json.return_value = {"choices": [{"message": {"content": content}}]}
            with mock.patch("bot.ai.requests.post", return_value=r) as post:
                self.assertEqual(ai.check_generated("t", "x", "p", b"jpg"), expected)
            body = post.call_args.kwargs["json"]
            self.assertEqual(body["messages"][0]["content"][1]["type"], "image_url")

    def test_prompt_asks_for_details(self):
        from bot.ai import AI
        ai = AI(Secrets(), load_network())
        seen = {}
        def complete(model, system, user, temperature=0.8):
            seen["user"] = user
            return json.dumps({"topic": "t", "text": "Достаточно длинный текст поста для проверки работы.",
                               "image_prompt": "p", "image_queries": ["a"]}, ensure_ascii=False)
        ai.complete = complete
        ai.generate_post(self.ch, self.ch.rubrics[0], "завтра", [])
        self.assertIn("АТМОСФЕРНУЮ ИЛЛЮСТРАЦИЮ", seen["user"])
        self.assertIn("плохо — мелкие детали", seen["user"])
        self.assertIn("в салоне автомобиля — руль", seen["user"])

    def test_check_prompt_has_logic_errors(self):
        from bot.ai import AI
        ai = AI(Secrets(yandex_api_key="K", yandex_folder_id="f"), load_network())
        r = mock.Mock(status_code=200)
        r.json.return_value = {"choices": [{"message": {"content": "НЕТ: в салоне нет руля"}}]}
        with mock.patch("bot.ai.requests.post", return_value=r) as post:
            self.assertEqual(ai.check_generated("t", "x", "p", b"j"), (False, "в салоне нет руля"))
        prompt = post.call_args.kwargs["json"]["messages"][0]["content"][0]["text"]
        self.assertIn("нет руля", prompt)
        self.assertIn("коробки", prompt)


class TestCheck(Base):
    def test_check_report(self):
        from bot import diagnostics
        from bot.images import Candidate
        bot = self.ctx.bot
        orig = bot.call
        def call(method, files=None, **params):
            if method == "getMe":
                return {"id": 42, "username": "my_DYmedia_helper_bot"}
            if method == "getChatMember":
                if params["chat_id"] == "@diy_chan":
                    return {"status": "member"}
                return {"status": "administrator", "can_post_messages": True}
            return orig(method, files=files, **params)
        bot.call = call
        self.ctx.network.image_check = True
        self.ctx.ai.pick_image = lambda t, x, c: 0
        self.ctx.channel("food").chat_id = ""
        with mock.patch("bot.images.search_pixabay", return_value=[Candidate("pixabay:1", "u", 1)]), \
                mock.patch("bot.images.search_wikimedia", return_value=[]), \
                mock.patch("bot.images.search_openverse", side_effect=RuntimeError("down")):
            text = diagnostics.run_checks(self.ctx)
        self.assertIn("✅ Бот @my_DYmedia_helper_bot на связи", text)
        self.assertIn("✅ Тексты <code>deepseek-v4.1-flash</code>", text)
        self.assertIn("✅ Проверка картинок ИИ", text)
        self.assertIn("❌ 🔨 Руки из плеч: бот не администратор", text)
        self.assertIn("✅ Склад медиа", text)
        self.assertIn("➖ Pexels: нет ключа", text)
        self.assertIn("➖ YandexART выключен", text)
        self.assertIn("Ждут подключения (1): Вкусно и полезно", text)
        self.assertIn("Проблем:", text)

    def test_check_text_model_error(self):
        from bot import diagnostics
        def boom(*a, **k):
            from bot.ai import AIError
            raise AIError("Yandex HTTP 403: permission denied")
        self.ctx.ai.complete = boom
        lines = diagnostics.check_text_models(self.ctx)
        self.assertTrue(lines[0].startswith("❌"))
        self.assertIn("403", lines[0])

    def test_check_command(self):
        self.ctx.bot.pending_updates = [{"update_id": 5, "message": {
            "text": "/check", "chat": {"id": int(ADMIN)}, "from": {"id": int(ADMIN)}}}]
        with mock.patch("bot.diagnostics.run_checks", return_value="🩺 отчёт"):
            approvals.process_updates(self.ctx)
        self.assertIn("🩺 отчёт", [p.get("text") for p in self.ctx.bot.sent_to(int(ADMIN))])


class TestVideo(Base):
    def _stock(self):
        from bot.images import Candidate
        def dl(url, max_bytes):
            f = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
            f.write(b"x")
            f.close()
            return f.name
        self.p_stock.stop()
        self.p_stock = mock.patch("bot.stockvideo.find_stock_videos", side_effect=lambda *a, **k: [
            Candidate(f"pixabayvideo:{random.random()}", "https://cdn/v.mp4", 2, preview="https://cdn/t.jpg",
                      via="tags")])
        self.p_stock.start()
        return mock.patch("bot.stockvideo.download_video", side_effect=dl)

    def test_stock_video_when_no_sources(self):
        self.p_vid.stop()
        with mock.patch("bot.reposter.find_videos", return_value=[]), self._stock():
            q = None
            for seed in range(6):  # видео-рубрика выпадает случайно — ищем день, где она есть
                self.ctx.rng = random.Random(seed)
                q = planner.plan_day(self.ctx, date(2026, 10, 24) + timedelta(days=seed), force=True)
                vids = [p for p in q["posts"] if p["kind"] == "video"]
                if any(p["channel"] == "nature" for p in vids):
                    break
        self.p_vid.start()
        vids = [p for p in q["posts"] if p["kind"] == "video"]
        self.assertTrue(vids)
        self.assertTrue(all(p["video_from"] == "stock" for p in vids))
        self.assertFalse(any(p["channel"] == "afterdark" for p in vids))  # 18+ — без стока
        self.assertTrue(all(p["source"].startswith("pixabayvideo:") for p in vids))

    def test_forwarded_video_goes_to_chosen_channel(self):
        admin = int(ADMIN)
        self.ctx.bot.pending_updates = [{"update_id": 1, "message": {
            "message_id": 77, "chat": {"id": admin}, "from": {"id": admin}, "caption": "Смешной кот",
            "video": {"file_id": "BAAD", "file_unique_id": "u77", "file_size": 3_000_000},
            "forward_origin": {"type": "channel", "chat": {"username": "cats_world", "title": "Cats"},
                               "message_id": 555}}}]
        approvals.process_updates(self.ctx)
        ask = [p for p in self.ctx.bot.sent_to(admin) if "В какой канал" in (p.get("text") or "")]
        self.assertTrue(ask)
        datas = [b["callback_data"] for row in ask[0]["reply_markup"]["inline_keyboard"] for b in row]
        self.assertIn("q:77:food", datas)
        self.assertIn("q:77:-", datas)
        self.ctx.bot.pending_updates = [{"update_id": 2, "callback_query": {
            "id": "c1", "from": {"id": admin}, "data": "q:77:food",
            "message": {"chat": {"id": admin}, "message_id": 78}}}]
        approvals.process_updates(self.ctx)
        self.assertEqual(len(self.ctx.state.inbox()["channels"]["food"]), 1)
        self.assertIn("food", approvals.videos_text(self.ctx).replace("Вкусно", "food"))
        q = self.plan()
        food = [p for p in q["posts"] if p["channel"] == "food"]
        self.assertEqual(len(food), 3)
        vid = [p for p in food if p["kind"] == "video"]
        self.assertEqual(len(vid), 1)
        self.assertEqual(vid[0]["media"], "BAAD")
        self.assertIn("https://t.me/cats_world/555", vid[0]["text"])
        self.assertEqual(self.ctx.state.inbox()["channels"]["food"], [])

    def test_video_cancel_and_sources(self):
        admin = int(ADMIN)
        self.ctx.bot.pending_updates = [{"update_id": 1, "message": {
            "message_id": 5, "chat": {"id": admin}, "from": {"id": admin},
            "document": {"file_id": "DOC", "mime_type": "video/mp4"}}}]
        approvals.process_updates(self.ctx)
        self.ctx.bot.pending_updates = [{"update_id": 2, "callback_query": {
            "id": "c", "from": {"id": admin}, "data": "q:5:-", "message": {"chat": {"id": admin}, "message_id": 6}}}]
        approvals.process_updates(self.ctx)
        inbox = self.ctx.state.inbox()
        self.assertEqual(inbox["pending"], {})
        self.assertEqual(inbox["channels"], {})
        fs = approvals.forward_source
        self.assertEqual(fs({"forward_origin": {"type": "channel", "chat": {"title": "Закрытый"}}}), "Закрытый")
        self.assertEqual(fs({"forward_origin": {"type": "hidden_user", "sender_user_name": "Иван"}}), "Иван")
        self.assertEqual(fs({}), "")

    def test_non_admin_video_ignored(self):
        self.ctx.bot.pending_updates = [{"update_id": 1, "message": {
            "message_id": 5, "chat": {"id": 9}, "from": {"id": 9}, "video": {"file_id": "X"}}}]
        approvals.process_updates(self.ctx)
        self.assertEqual(self.ctx.state.inbox()["pending"], {})

    def test_stock_search_picks_fitting_file(self):
        from bot import stockvideo
        hit = {"id": 1, "tags": "lion, savanna", "duration": 15, "videos": {
            "large": {"url": "L", "size": 90_000_000, "width": 3840, "thumbnail": "t"},
            "medium": {"url": "M", "size": 20_000_000, "width": 1920, "thumbnail": "tm"}}}
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"hits": [hit, dict(hit, id=2, duration=200)]}
        resp.raise_for_status = lambda: None
        with mock.patch("bot.stockvideo.requests.get", return_value=resp) as g:
            c = stockvideo.search_pixabay_videos("lion walking", "k", 45 * 1024 * 1024)
        self.assertEqual(len(c), 1)  # 200-секундный ролик отброшен
        self.assertEqual(c[0].url, "M")
        self.assertEqual(c[0].preview, "tm")
        self.assertEqual(c[0].score, 1)
        self.assertIn("videos", g.call_args[0][0])


class TestCompletePlan(Base):
    def test_new_channel_added_to_existing_plan(self):
        food = self.ctx.channel("food")
        chat = food.chat_id
        food.chat_id = ""
        q = self.plan()
        self.assertFalse(any(p["channel"] == "food" for p in q["posts"]))
        first = q["posts"][0]
        first["status"] = "rejected"
        self.ctx.state.save_queue(q)
        n_before = len(q["posts"])
        food.chat_id = chat
        q2 = self.plan()
        self.assertEqual(len([p for p in q2["posts"] if p["channel"] == "food"]), 3)
        self.assertEqual(len(q2["posts"]), n_before + 3)
        self.assertEqual(next(p for p in q2["posts"] if p["id"] == first["id"])["status"], "rejected")
        sent = [p.get("text") or "" for p in self.ctx.bot.sent_to(ADMIN)]
        self.assertTrue(any("добавлены новые каналы" in t for t in sent))
        q3 = self.plan()  # третий запуск — добавлять нечего
        self.assertEqual(len(q3["posts"]), n_before + 3)


class TestBudget(Base):
    def test_plan_level_and_rushed_note(self):
        self.ctx.network.plan_budget_minutes = 30
        self.assertEqual(self.ctx.plan_level(), 0)
        self.ctx.plan_started = time.time() - 31 * 60
        self.assertEqual(self.ctx.plan_level(), 1)
        self.ctx.plan_started = time.time() - 61 * 60
        self.assertEqual(self.ctx.plan_level(), 2)
        self.ctx.plan_started = None
        self.ctx.network.plan_budget_minutes = 0  # минимум 5 минут — без задержки план всё равно идёт как обычно
        q = self.plan()
        self.assertFalse(any("дольше" in e for e in q["errors"]))

    def test_late_posts_not_flooded(self):
        q = self.plan()
        for p in q["posts"]:
            p["status"] = "approved"
        late = datetime.fromisoformat(q["posts"][0]["time"]) + timedelta(hours=5)
        with mock.patch("bot.publisher.now", return_value=late):
            publisher.publish_due(self.ctx, q)
        self.assertEqual(q["posts"][0]["status"], "expired")


class TestNormalize(unittest.TestCase):
    def test_chat_id_forms(self):
        from bot.settings import normalize_chat_id as n
        self.assertEqual(n("pod_kapotom"), "@pod_kapotom")
        self.assertEqual(n("@pod_kapotom"), "@pod_kapotom")
        self.assertEqual(n("https://t.me/pod_kapotom"), "@pod_kapotom")
        self.assertEqual(n("t.me/pod_kapotom/12"), "@pod_kapotom")
        self.assertEqual(n(" -1001234567890 "), "-1001234567890")
        self.assertEqual(n(-1001234567890), "-1001234567890")
        self.assertEqual(n(""), "")
        self.assertEqual(n("https://t.me/+AbCd"), "+AbCd")

    def test_vision_reads_reasoning_when_content_empty(self):
        from bot.ai import AI
        from bot.images import Candidate
        ai = AI(Secrets(yandex_api_key="K", yandex_folder_id="f"), load_network())
        img = mock.Mock(content=b"jpg", headers={"Content-Type": "image/jpeg"})
        img.raise_for_status = lambda: None
        answer = mock.Mock(status_code=200)
        answer.json.return_value = {"choices": [{"message": {"content": "",
                                                             "reasoning_content": "Фото 1 — бачок... выбираю 2"}}]}
        with mock.patch("bot.ai.requests.get", return_value=img), \
                mock.patch("bot.ai.requests.post", return_value=answer) as post:
            n = ai.pick_image("t", "x", [Candidate("a", "u1", 1), Candidate("b", "u2", 1)])
        self.assertEqual(n, 1)
        self.assertGreaterEqual(post.call_args.kwargs["json"]["max_tokens"], 1000)


class TestChannelId(Base):
    def test_id_command_in_channel(self):
        self.ctx.bot.pending_updates = [{"update_id": 7, "channel_post": {
            "text": "/id", "chat": {"id": -1001234, "type": "channel"}}}]
        approvals.process_updates(self.ctx)
        self.assertIn("-1001234", self.ctx.bot.sent_to(-1001234, "sendMessage")[-1]["text"])


class TestReport(Base):
    def test_report(self):
        self.plan(date.today())
        with mock.patch("bot.reposter.channel_views", return_value={"posts": 3, "avg": 1200, "max": 5000}):
            text = report.send_report(self.ctx)
        self.assertIn("Отчёт за неделю", text)
        self.assertIn("Под капотом", text)
        self.assertIn("1 200", text)


if __name__ == "__main__":
    unittest.main()
