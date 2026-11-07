"""Weekly DeepSeek cost and balance report, sent to the admin chat on Sundays."""

from datetime import datetime, timedelta, timezone
from html import escape

from .app import App

JOB_NAMES = {"unicorn_prepare": "🦄 Школа единорогов", "radar": "🚨 Инвест радар"}
CURRENCY = {"USD": "$", "CNY": "¥"}


def format_balance(data: dict) -> str:
    infos = data.get("balance_infos") or []
    if not infos:
        return "нет данных"
    parts = []
    for info in infos:
        sign = CURRENCY.get(info.get("currency"), info.get("currency", ""))
        parts.append(f"{sign}{float(info.get('total_balance') or 0):.2f}")
    text = " / ".join(parts)
    if data.get("is_available") is False:
        text += " ⚠️ недостаточно для вызовов API"
    return text


def weekly_report(app: App, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=7)).isoformat(timespec="seconds")
    jobs = app.store.usage_by_job(since)
    total = sum(j["cost"] for j in jobs.values())
    lines = ["📊 <b>DeepSeek: отчёт за неделю</b>", "",
             f"Расходы за 7 дней: <b>${total:.2f}</b> <i>(оценка по ночным ценам)</i>"]
    for job, data in sorted(jobs.items(), key=lambda kv: -kv[1]["cost"]):
        lines.append(f"• {escape(JOB_NAMES.get(job, job))}: ${data['cost']:.2f} · запусков {data['runs']} · "
                     f"поисков {data['searches']} · страниц {data['fetches']}")
    try:
        balance = format_balance(app.deepseek.balance())
    except Exception as exc:  # noqa: BLE001
        balance = f"не удалось получить ({type(exc).__name__})"
    lines += ["", f"💰 Остаток на счёте API: <b>{escape(balance)}</b>"]
    if total > 0 and "$" in balance:
        try:
            usd = float(balance.split("$")[1].split()[0].split("/")[0])
            weeks = usd / total
            lines.append(f"Хватит примерно на {weeks:.0f} нед. при таком расходе")
        except (ValueError, IndexError):
            pass
    return "\n".join(lines)


def send_weekly_report(app: App) -> str:
    if not app.settings.telegram_admin_chat_id:
        return "no_admin_chat"
    app.telegram.send(app.settings.telegram_admin_chat_id, weekly_report(app))
    return "sent"
