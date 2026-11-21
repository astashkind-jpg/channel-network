"""Offline unit tests for the fleet watchdog (no GitHub credentials needed)."""
import unittest
from unittest.mock import patch
from scripts import fleet_watchdog as watchdog


def run(name, conclusion, event="schedule"):
    return {"name": name, "conclusion": conclusion, "event": event,
            "status": "completed", "updated_at": "2026-11-21T00:00:00Z",
            "html_url": "https://github.com/example/actions/runs/1"}


class WatchdogTests(unittest.TestCase):
    @patch.object(watchdog, "get_json")
    def test_success(self, fetch):
        fetch.return_value = {"workflow_runs": [run("Publish", "success")]}
        message, ok = watchdog.check("news-bot")
        self.assertTrue(ok)
        self.assertIn("conclusion=success", message)

    @patch.object(watchdog, "get_json")
    def test_failure(self, fetch):
        fetch.return_value = {"workflow_runs": [run("Publish", "failure")]}
        _, ok = watchdog.check("news-bot")
        self.assertFalse(ok)

    @patch.object(watchdog, "get_json")
    def test_ignores_self_and_pr(self, fetch):
        fetch.return_value = {"workflow_runs": [
            run("Telegram fleet watchdog", "success"),
            run("PR test", "success", "pull_request"),
            run("Publish", "timed_out")]}
        _, ok = watchdog.check("channel-network")
        self.assertFalse(ok)

    @patch.object(watchdog, "get_json")
    def test_no_runs(self, fetch):
        fetch.return_value = {"workflow_runs": []}
        _, ok = watchdog.check("flight-monitor")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
