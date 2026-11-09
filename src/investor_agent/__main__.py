"""Manual runs on the server:  python -m investor_agent prepare|publish|poll|poll-close|radar|digest|glossary|status [--date YYYY-MM-DD]

«Разгадка бизнеса»: riddle (prepare), riddle-publish, riddle-brief (video brief, media and YouTube manifests, status).

poll: post the readers' topic poll for the issue on --date (default: tomorrow) now; poll-close: tally it.
digest: the Invest Radar day digest now (only when the last Moscow day had 3+ alerts);
glossary: create or refresh the pinned glossary post and its Telegraph page."""

import argparse
import json
import logging

from .app import build_app
from .config import load_settings
from . import glossary
from .radar import digest as radar_digest
from .radar import pipeline as radar
from .riddle import pipeline as riddle
from .unicorn import pipeline as unicorn
from .unicorn import poll


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx INFO logs full URLs, incl. api_key=
    parser = argparse.ArgumentParser(prog="investor_agent")
    parser.add_argument("command", choices=["prepare", "publish", "poll", "poll-close", "radar", "digest", "glossary", "status",
                                                "riddle", "riddle-publish", "riddle-brief"])
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
    elif args.command == "digest":
        print(radar_digest.run(app))
    elif args.command == "riddle":
        print(riddle.prepare(app, args.date))
    elif args.command == "riddle-publish":
        print(riddle.publish(app, args.date))
    elif args.command == "riddle-brief":
        draft = app.store.draft_get(riddle.draft_key(args.date or riddle.issue_date()))
        report = (draft or {}).get("payload", {}).get("report", {})
        print(json.dumps({"status": draft and draft["status"], "page_url": draft and draft.get("page_url"),
                          "concept": report.get("concept"), "video_brief": report.get("video_brief"),
                          "fact_check": report.get("fact_check"), "cis": report.get("cis"),
                          "publication": (draft or {}).get("payload", {}).get("status"),
                          "manifests": (draft or {}).get("payload", {}).get("manifests")}, ensure_ascii=False, indent=2))
    elif args.command == "glossary":
        print(glossary.sync(app))
    else:
        draft = app.store.draft_get(args.date or unicorn.issue_date())
        print(json.dumps({"publish_mode": app.settings.publish_mode,
                          "unicorn": draft and {k: draft[k] for k in ("status", "page_url", "error")},
                          "reader_topic": app.store.get(poll.topic_key(args.date or unicorn.issue_date())),
                          "radar_last_ok": app.store.last_ok_window_end()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
