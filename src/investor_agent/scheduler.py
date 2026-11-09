"""Beijing-time scheduler. DeepSeek jobs are moved out of peak windows; Telegram posting is not."""

import logging
import threading
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from .app import App
from .radar import digest as radar_digest
from .radar import pipeline as radar
from .report import send_weekly_report
from .riddle import pipeline as riddle
from .timeutil import BEIJING, PeakHoursError, is_peak, next_offpeak, now_bj
from .unicorn import pipeline as unicorn
from .unicorn import poll as unicorn_poll

log = logging.getLogger(__name__)

UNICORN_POLL_CLOSE_TIME = unicorn_poll.CLOSE_TIME
UNICORN_PREPARE_TIMES = ((4, 30),)   # 30 min after the poll closes; NOT earlier, no advance preparation
UNICORN_RETRY_HOURS = 3              # after a failure: try again every 3 hours (peak windows push it out further)
UNICORN_RETRY_UNTIL = 22             # no new attempts from 22:00; tomorrow's 04:30 starts the next issue
UNICORN_PUBLISH_TIME = (10, 0)
RIDDLE_PREPARE_TIMES = ((0, 30),)    # «Разгадка бизнеса»: whole night off-peak, away from the 04:30 Unicorn job
RIDDLE_RETRY_UNTIL = 13              # retries every 3 h until 13:00 (the weekday peak window starts at 13:45)
RIDDLE_PUBLISH_TIME = (16, 0)        # 11:00 Moscow
RADAR_DIGEST_TIME = radar_digest.DIGEST_TIME_BEIJING   # 22:00 Moscow
RADAR_EVERY_HOURS = 3
WEEKLY_REPORT = (6, 20, 0)   # Sunday 20:00 Beijing


def next_daily(now: datetime, times: tuple) -> datetime:
    now = now.astimezone(BEIJING)
    candidates = []
    for day in (0, 1):
        for hh, mm in times:
            dt = (now + timedelta(days=day)).replace(hour=hh, minute=mm, second=0, microsecond=0)
            if dt > now:
                candidates.append(dt)
    return min(candidates)


def next_radar(now: datetime) -> datetime:
    """Next 3-hour slot (00, 03, ... Beijing), shifted out of peak windows."""
    now = now.astimezone(BEIJING)
    base = now.replace(minute=0, second=0, microsecond=0)
    slot = base + timedelta(hours=(RADAR_EVERY_HOURS - base.hour % RADAR_EVERY_HOURS) % RADAR_EVERY_HOURS)
    while slot <= now:
        slot += timedelta(hours=RADAR_EVERY_HOURS)
    return next_offpeak(slot) if is_peak(slot) else slot


def next_weekly(now: datetime) -> datetime:
    weekday, hh, mm = WEEKLY_REPORT
    now = now.astimezone(BEIJING)
    dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(days=(weekday - now.weekday()) % 7)
    return dt if dt > now else dt + timedelta(days=7)


@dataclass
class Job:
    name: str
    fn: Callable[[], object]
    next_fn: Callable[[datetime], datetime]
    uses_deepseek: bool = True
    retry_fn: Callable[[object, datetime], datetime | None] | None = None  # result -> time of a retry, or None
    next_run: datetime | None = None
    running: bool = field(default=False)


class Scheduler:
    def __init__(self, app: App, clock: Callable[[], datetime] = now_bj):
        self.app, self.clock = app, clock
        self._stop = threading.Event()
        self.jobs = [
            Job("unicorn_poll_close", lambda: unicorn_poll.close(app, unicorn.issue_date()),
                lambda n: next_daily(n, (UNICORN_POLL_CLOSE_TIME,)), uses_deepseek=False),
            Job("unicorn_prepare", lambda: unicorn.prepare(app), lambda n: next_daily(n, UNICORN_PREPARE_TIMES),
                retry_fn=self._prepare_retry),
            Job("unicorn_publish", lambda: unicorn.publish(app), lambda n: next_daily(n, (UNICORN_PUBLISH_TIME,)),
                uses_deepseek=False),
            Job("riddle_prepare", lambda: riddle.prepare(app), lambda n: next_daily(n, RIDDLE_PREPARE_TIMES),
                retry_fn=self._riddle_retry),
            Job("riddle_publish", lambda: riddle.publish(app), lambda n: next_daily(n, (RIDDLE_PUBLISH_TIME,)),
                uses_deepseek=False),
            Job("radar", lambda: radar.run(app), next_radar),
            Job("radar_digest", lambda: radar_digest.run(app), lambda n: next_daily(n, (RADAR_DIGEST_TIME,)),
                uses_deepseek=False),
            Job("weekly_report", lambda: send_weekly_report(app), next_weekly, uses_deepseek=False),
        ]
        now = clock()
        for job in self.jobs:
            job.next_run = job.next_fn(now)
        self._catch_up(now)

    def _job(self, name: str) -> Job:
        return next(j for j in self.jobs if j.name == name)

    def _prepare_retry(self, result: object, now: datetime) -> datetime | None:
        """A failed night preparation is retried every 3 hours until 22:00 (docs/RULES.md section 2)."""
        if result != "failed":
            return None
        nxt = now + timedelta(hours=UNICORN_RETRY_HOURS)
        if nxt.date() != now.date() or nxt.hour >= UNICORN_RETRY_UNTIL:
            self.app.notify_admin("Школа единорогов: выпуск на сегодня не подготовлен, повторных попыток больше нет "
                                  "(следующая — завтра в 04:30).")
            return None
        return nxt

    def _riddle_retry(self, result: object, now: datetime) -> datetime | None:
        """A failed or BLOCKED «Разгадка бизнеса» is prepared again every 3 hours until 13:00 Beijing."""
        if result not in ("failed", "blocked"):
            return None
        nxt = now + timedelta(hours=UNICORN_RETRY_HOURS)
        if nxt.date() != now.date() or nxt.hour >= RIDDLE_RETRY_UNTIL:
            self.app.notify_admin("Разгадка бизнеса: выпуск на сегодня не подготовлен, повторных попыток больше нет "
                                  "(следующая — завтра в 00:30 по Пекину).")
            return None
        return nxt

    def _catch_up_riddle(self, now: datetime) -> None:
        draft = self.app.store.draft_get(riddle.draft_key(riddle.issue_date(now)))
        minutes = now.hour * 60 + now.minute
        if draft and draft["status"] == "ready" and (now.hour, now.minute) >= RIDDLE_PUBLISH_TIME:
            self._job("riddle_publish").next_run = now
        elif (not draft or draft["status"] in ("failed", "blocked")) and 30 <= minutes < RIDDLE_RETRY_UNTIL * 60:
            self._job("riddle_prepare").next_run = now

    def _catch_up(self, now: datetime) -> None:
        """After a restart: publish today's ready report if it was missed; redo a missing or failed preparation."""
        self._catch_up_riddle(now)
        minutes = now.hour * 60 + now.minute
        if not (270 <= minutes < UNICORN_RETRY_UNTIL * 60):
            return
        draft = self.app.store.draft_get(unicorn.issue_date(now))
        if draft and draft["status"] == "ready" and now.hour >= 10:
            self._job("unicorn_publish").next_run = now
        elif not draft or draft["status"] == "failed":
            self._job("unicorn_prepare").next_run = now

    def _execute(self, job: Job) -> None:
        try:
            result = job.fn()
            log.info("job %s -> %s", job.name, result)
            self._after(job, result)
        except PeakHoursError as exc:
            log.info("job %s deferred: %s", job.name, exc)
            job.next_run = next_offpeak(self.clock()) + timedelta(minutes=1)
        except Exception:  # noqa: BLE001
            log.exception("job %s crashed", job.name)
            self._after(job, "failed")
        finally:
            job.running = False

    def _after(self, job: Job, result: object) -> None:
        now = self.clock()
        retry = job.retry_fn(result, now) if job.retry_fn else None
        job.next_run = retry or job.next_fn(now)
        # an issue that became ready after 10:00 (a retry) goes out at once, followed by the readers' poll
        if job.name == "unicorn_prepare" and result == "ready" and now.hour >= 10:
            self._job("unicorn_publish").next_run = now
        if job.name == "riddle_prepare" and result == "ready" and (now.hour, now.minute) >= RIDDLE_PUBLISH_TIME:
            self._job("riddle_publish").next_run = now

    def tick(self) -> None:
        now = self.clock()
        for job in self.jobs:
            if job.running or job.next_run is None or now < job.next_run:
                continue
            if job.uses_deepseek and is_peak(now):
                job.next_run = next_offpeak(now) + timedelta(minutes=1)
                continue
            job.running = True
            threading.Thread(target=self._execute, args=(job,), name=job.name, daemon=True).start()

    def status(self) -> dict:
        return {j.name: {"next_run": j.next_run.isoformat() if j.next_run else None, "running": j.running}
                for j in self.jobs}

    def run_forever(self, interval: float = 20) -> None:
        log.info("scheduler started: %s", self.status())
        while not self._stop.is_set():
            self.tick()
            _time.sleep(interval)

    def stop(self) -> None:
        self._stop.set()
