"""Offline unit tests for the fleet watchdog (no GitHub credentials needed)."""
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from scripts import fleet_watchdog as watchdog


def run(name, conclusion, event="schedule"):
    return {"created_at": datetime.now(timezone.utc).isoformat(), "name": name, "conclusion": conclusion, "event": event,
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
    def test_other_workflow_failure_not_masked(self, fetch):
        successful = run("News", "success")
        successful["workflow_id"] = 1
        failed = run("Facts", "failure")
        failed["workflow_id"] = 2
        fetch.return_value = {"workflow_runs": [successful, failed]}
        message, ok = watchdog.check("vzolote-bot")
        self.assertFalse(ok)
        self.assertIn("UNHEALTHY: Facts=failure", message)
        self.assertIn("https://github.com/example/actions/runs/1", message)

    @patch.object(watchdog, "get_json")
    def test_cancelled_publication_is_unhealthy(self, fetch):
        fetch.return_value = {"workflow_runs": [run("Publish", "cancelled")]}
        message, ok = watchdog.check("flight-monitor")
        self.assertFalse(ok)
        self.assertIn("UNHEALTHY: Publish=cancelled", message)
        self.assertIn("unhealthy_runs_in_last_100=1", message)

    @patch.object(watchdog, "get_json")
    def test_action_required_counted(self, fetch):
        fetch.return_value = {"workflow_runs": [run("Publish", "action_required")]}
        message, ok = watchdog.check("flight-monitor")
        self.assertFalse(ok)
        self.assertIn("unhealthy_runs_in_last_100=1", message)

    @patch.object(watchdog, "get_json")
    def test_startup_failure_is_unhealthy(self, fetch):
        fetch.return_value = {"workflow_runs": [run("Publish", "startup_failure")]}
        message, ok = watchdog.check("flight-monitor")
        self.assertFalse(ok)
        self.assertIn("UNHEALTHY: Publish=startup_failure", message)
        self.assertIn("unhealthy_runs_in_last_100=1", message)

    @patch.object(watchdog, "get_json")
    def test_manual_success_does_not_mask_missing_schedule(self, fetch):
        fetch.return_value = {"workflow_runs": [run("news-bot", "success", "workflow_dispatch")]}
        message, ok = watchdog.check("news-bot")
        self.assertFalse(ok)
        self.assertIn("NO_SCHEDULE_RUN", message)

    @patch.object(watchdog, "get_json")
    def test_stale_schedule_detected(self, fetch):
        old = run("tick", "success")
        old["created_at"] = "2026-11-04T04:57:00Z"
        fetch.return_value = {"workflow_runs": [old]}
        message, ok = watchdog.check("channel-network")
        self.assertFalse(ok)
        self.assertIn("STALE_SCHEDULE", message)

    @patch.object(watchdog, "get_json")
    def test_recent_schedule_healthy(self, fetch):
        fetch.return_value = {"workflow_runs": [run("news-bot", "success")]}
        _, ok = watchdog.check("news-bot")
        self.assertTrue(ok)

    @patch.object(watchdog, "get_json")
    def test_no_runs(self, fetch):
        fetch.return_value = {"workflow_runs": []}
        _, ok = watchdog.check("flight-monitor")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
