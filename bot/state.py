"""Состояние бота в JSON-файлах папки state/ (GitHub Actions коммитит её обратно в репозиторий)."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from .settings import ROOT


class State:
    def __init__(self, root: Path | None = None):
        self.root = root or (ROOT / "state")
        (self.root / "queue").mkdir(parents=True, exist_ok=True)
        (self.root / "history").mkdir(parents=True, exist_ok=True)

    # --- базовые операции ---
    def _read(self, path: Path, default):
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def _write(self, path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)

    # --- очередь постов на день ---
    def queue_path(self, d: date) -> Path:
        return self.root / "queue" / f"{d.isoformat()}.json"

    def load_queue(self, d: date) -> dict | None:
        return self._read(self.queue_path(d), None)

    def save_queue(self, q: dict) -> None:
        self._write(self.queue_path(date.fromisoformat(q["date"])), q)

    def active_queues(self, today: date) -> list[dict]:
        """Очереди за вчера, сегодня и завтра — всё, что ещё может быть не опубликовано."""
        result = []
        for delta in (-1, 0, 1):
            q = self.load_queue(today + timedelta(days=delta))
            if q:
                result.append(q)
        return result

    def cleanup(self, today: date, keep_days: int = 14) -> None:
        for p in (self.root / "queue").glob("*.json"):
            try:
                if date.fromisoformat(p.stem) < today - timedelta(days=keep_days):
                    p.unlink()
            except ValueError:
                continue

    # --- история канала: темы, использованные картинки и видео ---
    def history(self, key: str) -> dict:
        h = self._read(self.root / "history" / f"{key}.json", {})
        h.setdefault("topics", [])
        h.setdefault("images", [])
        h.setdefault("videos", [])
        h.setdefault("members", [])
        return h

    def save_history(self, key: str, h: dict, limit: int = 200) -> None:
        for k in ("topics", "images", "videos"):
            h[k] = h[k][-limit:]
        h["members"] = h["members"][-60:]
        self._write(self.root / "history" / f"{key}.json", h)

    # --- видео, которые вы переслали боту: ждут выбора канала (pending) и очереди каналов ---
    def inbox(self) -> dict:
        b = self._read(self.root / "inbox.json", {})
        b.setdefault("pending", {})
        b.setdefault("channels", {})
        return b

    def save_inbox(self, b: dict) -> None:
        # храним не больше 30 роликов, для которых канал ещё не выбран
        if len(b["pending"]) > 30:
            for k in sorted(b["pending"], key=lambda k: b["pending"][k].get("added", ""))[:-30]:
                b["pending"].pop(k, None)
        self._write(self.root / "inbox.json", b)

    # --- общее: offset getUpdates, пауза ---
    def meta(self) -> dict:
        m = self._read(self.root / "meta.json", {})
        m.setdefault("update_offset", 0)
        m.setdefault("paused", [])
        return m

    def save_meta(self, m: dict) -> None:
        self._write(self.root / "meta.json", m)
