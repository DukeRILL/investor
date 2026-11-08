"""Manual runs on the server:  python -m investor_agent prepare|publish|poll|poll-close|radar|digest|glossary|status [--date YYYY-MM-DD]

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
from .unicorn import pipeline as unicorn
from .unicorn import poll


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx INFO logs full URLs, incl. api_key=
    parser = argparse.ArgumentParser(prog="investor_agent")
    parser.add_argument("command", choices=["prepare", "publish", "poll", "poll-close", "radar", "digest", "glossary", "status", "growth-daily", "growth-report",
                                            "growth-updates", "growth-link", "growth-export", "growth-backfill", "reconcile"])
    parser.add_argument("--date", help="issue date YYYY-MM-DD (Beijing) for prepare/publish")
    parser.add_argument("--message-id", type=int, help="reconcile: id of the issue post that is in the channel")
    parser.add_argument("--not-sent", action="store_true", help="reconcile: the post is not in the channel")
    parser.add_argument("--source", help="growth-link: instagram|tiktok|youtube|vk|whatsapp|other")
    parser.add_argument("--out", default="data/export", help="growth-export directory")
    args = parser.parse_args()
    app = build_app(load_settings())
    if args.command == "prepare":
        print(unicorn.prepare(app, args.date))
    elif args.command == "publish":
        print(unicorn.publish(app, args.date))
    elif args.command == "poll":
        issue = args.date or poll.next_issue(unicorn.issue_date())
        app.store.set(f"unicorn_poll_intent:{issue}", "")  # a manual run: the owner has checked the channel
        options = poll.make_options(app, issue, app.store.recent_archive(14, issue))
        print(json.dumps({"issue": issue, "message_id": poll.post(app, issue, options), "options": options},
                         ensure_ascii=False))
    elif args.command == "poll-close":
        print(json.dumps(poll.close(app, args.date or unicorn.issue_date()), ensure_ascii=False))
    elif args.command == "radar":
        print(radar.run(app))
    elif args.command == "digest":
        print(radar_digest.run(app))
    elif args.command == "glossary":
        print(glossary.sync(app, force=True))  # manual run: the owner has checked the channel
    elif args.command == "reconcile":
        print(unicorn.reconcile(app, args.date or unicorn.issue_date(), args.message_id, args.not_sent))
    elif args.command.startswith("growth-"):
        from .growth import collect, report as greport  # noqa: PLC0415
        from .growth.links import db, make_link  # noqa: PLC0415

        if args.command == "growth-daily":
            print(greport.run_daily(app))
        elif args.command == "growth-report":
            print(greport.build_report(app))
        elif args.command == "growth-updates":
            print(json.dumps(collect.poll_updates(app)))
        elif args.command == "growth-export":
            print(json.dumps(greport.export(app, args.out)))
        elif args.command == "growth-backfill":
            print(greport.backfill(app))
        else:  # growth-link: an invite link for one external source and one issue; prints the link
            pub = db(app).publication_by_date(args.date or unicorn.issue_date())
            if not pub or not args.source:
                raise SystemExit("need a published --date and --source")
            link = make_link(app, pub["id"], pub["issue_date"], args.source, "manual")
            print(link.get("invite_link") or link["target_url"])
    else:
        draft = app.store.draft_get(args.date or unicorn.issue_date())
        with app.store._conn() as c:  # publication states that block a rollback (docs/GROWTH.md)
            pending = {r["key"].split(":", 1)[1]: r["value"] for r in c.execute(
                "SELECT key, value FROM kv WHERE key LIKE 'unicorn_publish_state:%' AND value != ''")}
        print(json.dumps({"publish_mode": app.settings.publish_mode, "unresolved_publish_states": pending,
                          "unicorn": draft and {k: draft[k] for k in ("status", "page_url", "error")},
                          "reader_topic": app.store.get(poll.topic_key(args.date or unicorn.issue_date())),
                          "radar_last_ok": app.store.last_ok_window_end()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
