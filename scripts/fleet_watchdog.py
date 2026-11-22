"""Read-only GitHub Actions health monitor for the four Telegram repositories.

No publishing, AI calls, repository writes or external dependencies.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

OWNER = "astashkind-jpg"
REPOS = ("flight-monitor", "news-bot", "vzolote-bot", "channel-network")
TOKEN = os.getenv("GH_TOKEN", "")
HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "telegram-fleet-watchdog"}
if TOKEN:
    HEADERS["Authorization"] = "Bearer " + TOKEN


def get_json(url):
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def check(repo):
    url = f"https://api.github.com/repos/{OWNER}/{repo}/actions/runs?per_page=100"
    data = get_json(url)
    runs = [r for r in data.get("workflow_runs", [])
            if r.get("event") != "pull_request"
            and r.get("name") != "Telegram fleet watchdog"]
    if not runs:
        return f"{repo}: no workflow runs found", False
    recent = runs[0]
    name = recent.get("name", "?")
    status = recent.get("status", "?")
    conclusion = recent.get("conclusion") or "pending"
    updated = recent.get("updated_at") or "?"
    failed = [r for r in runs if r.get("conclusion") in ("failure", "timed_out")]
    link = recent.get("html_url", "")
    # Inspect the most recent run of each workflow, not just the newest run
    # overall: a successful news workflow must not hide a failing facts workflow.
    latest_by_workflow = {}
    for run in runs:
        key = run.get("workflow_id") or run.get("name") or "unknown"
        latest_by_workflow.setdefault(key, run)
    bad = ("failure", "timed_out", "action_required")
    unhealthy = [r for r in latest_by_workflow.values() if r.get("conclusion") in bad]
    ok = not unhealthy
    details = "; ".join(
        f"{r.get('name', '?')}={r.get('conclusion')} {r.get('html_url', '')}"
        for r in unhealthy
    )
    return (f"{repo}: latest={name} status={status} conclusion={conclusion} "
            f"updated={updated}; workflows_checked={len(latest_by_workflow)} "
            f"failures_in_last_100={len(failed)} {link}"
            + (f"; UNHEALTHY: {details}" if details else "")), ok


def main():
    print("Read-only audit:", datetime.now(timezone.utc).isoformat())
    healthy = True
    for repo in REPOS:
        try:
            result, ok = check(repo)
            print(result)
            healthy = healthy and ok
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            print(f"{repo}: unable to check ({type(exc).__name__}: {exc})")
            healthy = False
    if not healthy:
        print("WARNING: at least one repository could not be checked", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
