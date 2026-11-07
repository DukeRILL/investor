"""Manual runs on the server:  python -m investor_agent prepare|publish|radar|status [--date YYYY-MM-DD]"""

import argparse
import json
import logging

from .app import build_app
from .config import load_settings
from .radar import pipeline as radar
from .unicorn import pipeline as unicorn


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="investor_agent")
    parser.add_argument("command", choices=["prepare", "publish", "radar", "status"])
    parser.add_argument("--date", help="issue date YYYY-MM-DD (Beijing) for prepare/publish")
    args = parser.parse_args()
    app = build_app(load_settings())
    if args.command == "prepare":
        print(unicorn.prepare(app, args.date))
    elif args.command == "publish":
        print(unicorn.publish(app, args.date))
    elif args.command == "radar":
        print(radar.run(app))
    else:
        draft = app.store.draft_get(args.date or unicorn.issue_date())
        print(json.dumps({"publish_mode": app.settings.publish_mode,
                          "unicorn": draft and {k: draft[k] for k in ("status", "page_url", "error")},
                          "radar_last_ok": app.store.last_ok_window_end()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
