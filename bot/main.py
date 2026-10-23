"""Точка входа.

python -m bot.main plan [--date YYYY-MM-DD] [--force]   — план на завтра + сводка
python -m bot.main tick                                  — кнопки, автоутверждение, публикация
python -m bot.main report                                — недельный отчёт
"""
from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta

from .core import Context
from .planner import plan_day
from .publisher import tick
from .report import send_report
from .util import now


def main(argv=None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="bot")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_plan = sub.add_parser("plan")
    p_plan.add_argument("--date", help="дата плана, по умолчанию завтра")
    p_plan.add_argument("--force", action="store_true", help="пересоздать план, если он уже есть")
    sub.add_parser("tick")
    sub.add_parser("report")
    args = parser.parse_args(argv)

    ctx = Context.from_env()
    if args.cmd == "plan":
        day = date.fromisoformat(args.date) if args.date else now().date() + timedelta(days=1)
        q = plan_day(ctx, day, force=args.force)
        logging.info("план на %s: %d постов, ошибок %d", day, len(q["posts"]), len(q.get("errors", [])))
    elif args.cmd == "tick":
        tick(ctx)
    elif args.cmd == "report":
        send_report(ctx)


if __name__ == "__main__":
    main()
