#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys

from rss_app.config import load_config
from rss_app.runner import Runner


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Generate RSS/Atom feeds and push them to GitHub."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--watch", dest="watch", action="store_true", help="continuous mode")
    group.add_argument("--once", dest="watch", action="store_false", help="run one cycle then exit")
    parser.set_defaults(watch=True)
    parser.add_argument("--config", default="config.json", help="path to config.json")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except Exception as e:
        print(f"[fatal] failed to load config: {e}", file=sys.stderr)
        return 0

    try:
        Runner(cfg).run(watch=args.watch)
    except Exception as e:
        print(f"[fatal] runner error: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

