"""Second pass over a prepared issue: numbers are checked against sources by a cheap model (docs/RULES.md §5).

Corrections need a source that was really opened; unconfirmed numbers are flagged on the page, never silently kept.
A failure here never stops the issue (the first-pass validation already passed).
"""

import logging

from ..app import App
from ..sources import SourceRegistry
from ..timeutil import PeakHoursError

log = logging.getLogger(__name__)

METRIC_FIELDS = {"value"}
ROUND_FIELDS = {"amount", "pre_money", "post_money", "date"}
UNCONFIRMED_NOTE = "не подтверждено вторым источником"
CACHE_HOURS = 24  # the pages were just fetched during research: the check re-reads them for free


def apply_checks(report: dict, checks: list, registry: SourceRegistry) -> dict:
    metrics, rounds = report.get("metrics") or [], report.get("rounds") or []
    summary = {"checked": 0, "corrected": 0, "unverified": []}
    for check in checks if isinstance(checks, list) else []:
        if not isinstance(check, dict):
            continue
        kind, field, status = check.get("kind"), check.get("field"), check.get("status")
        try:
            index = int(check.get("index"))
        except (TypeError, ValueError):
            continue
        items, allowed = (metrics, METRIC_FIELDS) if kind == "metric" else (rounds, ROUND_FIELDS)
        if kind not in ("metric", "round") or not 0 <= index < len(items) or field not in allowed:
            continue
        item = items[index]
        summary["checked"] += 1
        src = registry.get(str(check.get("source") or ""))
        if status == "corrected" and check.get("corrected") and src and registry.verified([src.id]):
            item[field] = str(check["corrected"]).strip()
            if kind == "metric":
                item["source"] = src.id
            summary["corrected"] += 1
        elif status == "unverifiable":
            if kind == "metric":
                note = str(item.get("note") or "")
                if UNCONFIRMED_NOTE not in note:
                    item["note"] = f"{note} · {UNCONFIRMED_NOTE}".strip(" ·")
                summary["unverified"].append(f"{item.get('label')}: {item.get('value')}")
            else:
                summary["unverified"].append(f"раунд {item.get('date', '')} {item.get('stage', '')}: {field} — {item.get(field)}")
    if summary["unverified"]:
        report.setdefault("unknowns", []).append("Не удалось независимо подтвердить: " + "; ".join(summary["unverified"]))
    report["factcheck"] = {k: summary[k] for k in ("checked", "corrected")} | {"unverified": len(summary["unverified"])}
    return summary


def run(app: App, report: dict, registry: SourceRegistry, company: dict) -> dict:
    """Returns the summary; {} when the check could not run. PeakHoursError propagates (the night job is retried)."""
    payload = {
        "company": company.get("name") or report.get("company_name"), "facts_line": report.get("facts_line"),
        "metrics": [{"index": i, **m} for i, m in enumerate(report.get("metrics") or [])],
        "rounds": [{"index": i, **r} for i, r in enumerate(report.get("rounds") or [])],
        "summary_opening": next((s.get("body", "")[:3000] for s in report.get("sections", []) if s.get("id") == "summary"), ""),
        "known_sources": registry.brief(),
    }
    try:
        result = app.deepseek.research(app.settings.deepseek_model_fast, app.prompt("unicorn_factcheck"), payload,
                                       registry, max_tool_calls=15, cache_ttl=CACHE_HOURS)
    except PeakHoursError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("fact check skipped: %s", type(exc).__name__)
        return {}
    summary = apply_checks(report, result.get("checks"), registry)
    log.info("fact check: %s checked, %s corrected, %s unverified", summary["checked"], summary["corrected"],
             len(summary["unverified"]))
    return summary
