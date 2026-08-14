"""CLI entry for cka_mock."""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .config import load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cka-mock",
        description="LLM-generated CKA mock exams on Minikube",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    p_new = sub.add_parser("new", help="generate and set up a new mock exam")
    p_new.add_argument("--topics", nargs="*", default=None, help="topics to focus on")
    p_new.add_argument("--questions", type=int, default=None, help="number of questions")
    p_new.add_argument("--duration", type=int, default=None, help="exam duration in minutes")
    p_new.add_argument("--difficulty", choices=["easy", "medium", "hard"], default=None)
    p_new.add_argument("--config", help="path to config file")

    p_grade = sub.add_parser("grade", help="grade the current exam")
    p_grade.add_argument("--config", help="path to config file")

    p_status = sub.add_parser("status", help="show exam timer and cluster context")
    p_status.add_argument("--config", help="path to config file")

    p_reset = sub.add_parser("reset", help="delete the exam cluster (exam history is kept)")
    p_reset.add_argument("--config", help="path to config file")

    p_replay = sub.add_parser("replay", help="re-run a past exam deterministically (no LLM)")
    p_replay.add_argument("exam_id", help="exam id to replay")
    p_replay.add_argument("--config", help="path to config file")

    p_list = sub.add_parser("list", help="list past exams")
    p_list.add_argument("--json", action="store_true", help="output as JSON")
    p_list.add_argument("--config", help="path to config file")

    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0

    from .exam import run_grade, run_list, run_new, run_replay, run_reset, run_status

    try:
        if args.cmd == "new":
            cfg = load_config(args.config)
            return run_new(
                cfg,
                topics_override=args.topics,
                questions_override=args.questions,
                difficulty_override=args.difficulty,
            )
        if args.cmd == "grade":
            return run_grade(load_config(args.config))
        if args.cmd == "status":
            return run_status(load_config(args.config))
        if args.cmd == "reset":
            return run_reset(load_config(args.config))
        if args.cmd == "replay":
            return run_replay(load_config(args.config), args.exam_id)
        if args.cmd == "list":
            return run_list(load_config(args.config), json_output=args.json)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI should not traceback
        print(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
