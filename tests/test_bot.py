"""Сквозные тесты без сети: Telegram, ИИ, Pixabay и репост подменены заглушками.

Запуск: python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import random
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from bot import approvals, core, planner, publisher, report
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
            "image_query": "test photo"}, ensure_ascii=False) + "\n```")

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
    return core.Context(network=load_network(), channels=channels, secrets=secrets, bot=bot,
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
        self.p_img = mock.patch("bot.images.find_image",
                                side_effect=lambda q, k, used, rng, dry_run=False: (str(rng.random()), "https://img/x.jpg"))
        self.p_vid = mock.patch("bot.reposter.find_videos", side_effect=fake_videos)
        self.p_img.start()
        self.p_vid.start()
        # у каналов с video-рубриками должны быть источники, иначе видео не ищется вовсе
        for c in self.ctx.channels:
            if any(r.type == "video" for r in c.rubrics):
                c.video_sources = ["@src"]

    def tearDown(self):
        self.p_img.stop()
        self.p_vid.stop()

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
                    self.assertEqual(p["kind"], "text")

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
            if p["channel"] == "afterdark":
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
            self.assertEqual(p["status"], "expired" if p["channel"] == "afterdark" else "published")

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
