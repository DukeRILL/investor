"""Web app (health check on 127.0.0.1) + background scheduler."""

import logging
import threading
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from .app import build_app
from .config import load_settings
from .scheduler import Scheduler
from .timeutil import now_bj
from .unicorn.pipeline import issue_date

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx INFO logs full URLs, incl. api_key=
log = logging.getLogger("investor_agent")

settings = load_settings()
state: dict = {}


@asynccontextmanager
async def lifespan(_app):
    app = build_app(settings)
    state["app"] = app
    if settings.scheduler_enabled:
        scheduler = Scheduler(app)
        state["scheduler"] = scheduler
        threading.Thread(target=scheduler.run_forever, name="scheduler", daemon=True).start()
    yield
    if "scheduler" in state:
        state["scheduler"].stop()


async def health(_request):
    app = state.get("app")
    draft = app.store.draft_get(issue_date()) if app else None
    return JSONResponse({
        "status": "ok",
        "publish_mode": settings.publish_mode,
        "deepseek_configured": bool(settings.deepseek_api_key),
        "telegram_configured": bool(settings.telegram_bot_token),
        "now_beijing": now_bj().isoformat(timespec="minutes"),
        "unicorn_today": draft["status"] if draft else None,
        "riddle_today": (lambda d: d["status"] if d else None)(app.store.draft_get(f"riddle:{issue_date()}") if app else None),
        "last_radar_window_end": app.store.last_ok_window_end() if app else None,
        "jobs": state["scheduler"].status() if "scheduler" in state else None,
    })


app = Starlette(routes=[Route("/health", health)], lifespan=lifespan)
