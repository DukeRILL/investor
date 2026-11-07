"""Manual runs on the server:  python -m investor_agent prepare|publish|poll|poll-close|radar|status [--date YYYY-MM-DD]

poll: post the readers' topic poll for the issue on --date (default: tomorrow) now; poll-close: tally it."""

import argparse
import json
import logging

from .app import build_app
from .config import load_settings
from .radar import pipeline as radar
from .unicorn import pipeline as unicorn
from .unicorn import poll


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx INFO logs full URLs, incl. api_key=
    parser = argparse.ArgumentParser(prog="investor_agent")
    parser.add_argument("command", choices=["prepare", "publish", "poll", "poll-close", "radar", "status"])
    parser.add_argument("--date", help="issue date YYYY-MM-DD (Beijing) for prepare/publish")
    args = parser.parse_args()
    app = build_app(load_settings())
    if args.command == "prepare":
        print(unicorn.prepare(app, args.date))
    elif args.command == "publish":
        print(unicorn.publish(app, args.date))
    elif args.command == "poll":
        issue = args.date or poll.next_issue(unicorn.issue_date())
        options = poll.make_options(app, issue, app.store.recent_archive(14, issue))
        print(json.dumps({"issue": issue, "message_id": poll.post(app, issue, options), "options": options},
                         ensure_ascii=False))
    elif args.command == "poll-close":
        print(json.dumps(poll.close(app, args.date or unicorn.issue_date()), ensure_ascii=False))
    elif args.command == "radar":
        print(radar.run(app))
    else:
        draft = app.store.draft_get(args.date or unicorn.issue_date())
        print(json.dumps({"publish_mode": app.settings.publish_mode,
                          "unicorn": draft and {k: draft[k] for k in ("status", "page_url", "error")},
                          "reader_topic": app.store.get(poll.topic_key(args.date or unicorn.issue_date())),
                          "radar_last_ok": app.store.last_ok_window_end()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
