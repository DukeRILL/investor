"""«Разгадка бизнеса»: the shared fact-check gate (`factgate`) in strict mode over the final article."""

from ..app import App
from ..factgate import STRICT, numbers, page_text, scale_problem  # noqa: F401 - re-exported for the pipeline
from ..factgate import run as gate_run
from ..sources import SourceRegistry


def article_payload(report: dict) -> dict:
    """The final text exactly as the reader gets it, with field paths the checker can point to."""
    keep = ("headline", "teaser", "sections", "mechanism_chain", "timeline", "charts", "cis")
    out = {k: report.get(k) for k in keep if report.get(k)}
    out["images"] = [{"caption": m.get("caption", ""), "type": m.get("type", ""), "source": m.get("source_url", "")}
                     for m in report.get("images") or []]
    return out


def run(app: App, report: dict, registry: SourceRegistry) -> dict:
    return gate_run(app, report, registry, article_payload, STRICT, label="riddle")
