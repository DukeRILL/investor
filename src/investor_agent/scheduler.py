"""Beijing-time scheduler. DeepSeek jobs are moved out of peak windows; Telegram posting is not."""

import logging
import threading
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from .app import App
from .radar import pipeline as radar
from .report import send_weekly_report
from .timeutil import BEIJING, PeakHoursError, is_peak, next_offpeak, now_bj
from .unicorn import pipeline as unicorn

log = logging.getLogger(__name__)

UNICORN_PREPARE_TIMES = ((3, 0), (5, 0), (7, 0))   # first run + two retries, all before 08:45
UNICORN_PUBLISH_TIME = (10, 0)
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
    next_run: datetime | None = None
    running: bool = field(default=False)


class Scheduler:
    def __init__(self, app: App, clock: Callable[[], datetime] = now_bj):
        self.app, self.clock = app, clock
        self._stop = threading.Event()
        self.jobs = [
            Job("unicorn_prepare", lambda: unicorn.prepare(app), lambda n: next_daily(n, UNICORN_PREPARE_TIMES)),
            Job("unicorn_publish", lambda: unicorn.publish(app), lambda n: next_daily(n, (UNICORN_PUBLISH_TIME,)),
                uses_deepseek=False),
            Job("radar", lambda: radar.run(app), next_radar),
            Job("weekly_report", lambda: send_weekly_report(app), next_weekly, uses_deepseek=False),
        ]
        now = clock()
        for job in self.jobs:
            job.next_run = job.next_fn(now)
        self._catch_up(now)

    def _catch_up(self, now: datetime) -> None:
        """After a restart between 10:00 and 22:00, publish today's ready report if it was missed."""
        if not (10 <= now.hour < 22):
            return
        draft = self.app.store.draft_get(unicorn.issue_date(now))
        if draft and draft["status"] == "ready":
            self.jobs[1].next_run = now

    def _execute(self, job: Job) -> None:
        try:
            result = job.fn()
            log.info("job %s -> %s", job.name, result)
            job.next_run = job.next_fn(self.clock())
        except PeakHoursError as exc:
            log.info("job %s deferred: %s", job.name, exc)
            job.next_run = next_offpeak(self.clock()) + timedelta(minutes=1)
        except Exception:  # noqa: BLE001
            log.exception("job %s crashed", job.name)
            job.next_run = job.next_fn(self.clock())
        finally:
            job.running = False

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
