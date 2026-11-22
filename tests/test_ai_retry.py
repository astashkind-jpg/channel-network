"""Regression tests for retry timing without network or AI usage."""
import unittest
from unittest.mock import patch

from bot.ai import AI, AIError


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.ai = object.__new__(AI)
        self.ai.dry_run = False

    @patch("bot.ai.time.sleep")
    def test_final_failure_does_not_sleep(self, sleep):
        with patch.object(self.ai, "_chat", side_effect=AIError("Yandex HTTP 429")) as chat:
            with self.assertRaises(AIError):
                self.ai.complete("model", "system", "prompt")
        self.assertEqual(chat.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [3, 6])

    @patch("bot.ai.time.sleep")
    def test_permanent_http_error_is_not_retried(self, sleep):
        with patch.object(self.ai, "_chat", side_effect=AIError("Yandex HTTP 422")) as chat:
            with self.assertRaises(AIError):
                self.ai.complete("model", "system", "prompt")
        self.assertEqual(chat.call_count, 1)
        sleep.assert_not_called()

    @patch("bot.ai.time.sleep")
    def test_success_after_transient_failure(self, sleep):
        with patch.object(self.ai, "_chat", side_effect=[AIError("Yandex HTTP 429"), "ok"]) as chat:
            self.assertEqual(self.ai.complete("model", "system", "prompt"), "ok")
        self.assertEqual(chat.call_count, 2)
        sleep.assert_called_once_with(3)


if __name__ == "__main__":
    unittest.main()
