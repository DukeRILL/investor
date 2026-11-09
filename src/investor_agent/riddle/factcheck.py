"""Fact-check gate of «Разгадка бизнеса» (owner's production prompt, 2026-11-09, §14–19; docs/RULES.md §5).

An independent DeepSeek pass extracts every material claim from the FINAL article and checks it against opened
sources (FACT LEDGER). Fixes are applied to the exact text, then the corrected claims are checked again. The server
recomputes every unit conversion itself (亿 / млн / млрд …): a scale mismatch is an ERROR whatever the model says.
The issue is READY only when no material claim is left in ERROR or UNVERIFIED.
"""

import logging
import re

from ..app import App
from ..sources import SourceRegistry
from ..timeutil import PeakHoursError
from ..typesafe import Jev

log = logging.getLogger(__name__)

STATUSES = ("VERIFIED", "UNVERIFIED", "ERROR")
CACHE_HOURS = 24          # the pages were read during research: re-reading them is free
MAX_TOOL_CALLS = 40
MAX_RECHECK_CALLS = 20
SCALE_TOLERANCE = 0.03    # 457.3 vs 457 is rounding; 4,573 vs 457.3 is a tenfold error
PERCENT_TOLERANCE = 0.15  # percentage points
# TypeSafe Jev, the second independent judge (owner, 2026-11-09 15:48 UTC): a VERIFIED claim stays VERIFIED only when
# Jev says the source quote supports it with high probability and confidence.
JEV_MIN_SUPPORT = 0.80
JEV_MIN_CONFIDENCE = 0.60
JEV_CONTRADICTED = 0.50
JEV_CRITERIA = {
    "supported": "The source excerpt states the same fact as the claim: the same value at the same scale, unit, "
                 "currency, reporting period, metric and company or segment (allowing only rounding and translation).",
    "contradicted": "The source excerpt states a different value, scale, unit, period, metric, segment or fact.",
    "insufficient": "The excerpt does not contain enough information to confirm or refute the claim.",
}
JEV_INSTRUCTIONS = ("A Russian business article makes the CLAIM below; the EXCERPT is copied from the cited source. "
                    "Does the excerpt prove the claim? Units: 1 亿 = 100 million; млн = million; млрд = billion; "
                    "юаней = yuan. Gross, operating, net and adjusted profit are different metrics.")

# order matters: longer / more specific markers first
_SCALES = [("трлн", 1e12), ("trillion", 1e12), ("млрд", 1e9), ("миллиард", 1e9), ("billion", 1e9), ("bn", 1e9),
           ("亿", 1e8), ("млн", 1e6), ("миллион", 1e6), ("million", 1e6), ("mln", 1e6), ("mn", 1e6),
           ("万", 1e4), ("тыс", 1e3), ("thousand", 1e3), ("千", 1e3)]
_CURRENCIES = [("CNY", ("юан", "yuan", "rmb", "cny", "元")), ("USD", ("доллар", "usd", "$", "dollar")),
               ("HKD", ("hkd", "гонконг")), ("EUR", ("евро", "eur", "€")), ("RUB", ("руб", "rub", "₽"))]
_PATH_RE = re.compile(r"([a-z_]+)(?:\[(\d+)\])?")


def number(value) -> float | None:
    """'4,573.0' / '457,3' / '1 999' / 369 → float. None when there is no number."""
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"[\s  ]", "", str(value or ""))
    m = re.search(r"-?\d[\d.,]*", text)
    if not m:
        return None
    raw = m.group(0).rstrip(".,")
    if "," in raw and "." in raw:  # 4,573.0 (thousands comma) or 4.573,0 (decimal comma)
        raw = raw.replace(",", "") if raw.rfind(".") > raw.rfind(",") else raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        head, _, tail = raw.rpartition(",")
        raw = raw.replace(",", "") if len(tail) == 3 and raw.count(",") >= 1 and len(head) <= 3 and "," not in head \
            and len(raw) > 5 else raw.replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def scale(unit: str) -> float:
    u = str(unit or "").lower()
    for marker, factor in _SCALES:
        if marker in u:
            return factor
    return 1.0


def currency(unit: str) -> str | None:
    u = str(unit or "").lower()
    for code, markers in _CURRENCIES:
        if any(m in u for m in markers):
            return code
    return None


def scale_problem(claim: dict) -> str | None:
    """Programmatic check of the conversion source → article (§16). None when consistent or not comparable."""
    src, art = number(claim.get("source_value")), number(claim.get("article_value"))
    su, au = str(claim.get("source_unit") or ""), str(claim.get("article_unit") or "")
    if src is None or art is None:
        return None
    pct_s = "%" in su or "процент" in su.lower() or "percent" in su.lower()
    pct_a = "%" in au or "процент" in au.lower()
    if pct_s or pct_a:
        if not (pct_s and pct_a):
            return None
        return None if abs(src - art) <= PERCENT_TOLERANCE else f"источник {src}%, в статье {art}%"
    cs, ca = currency(su), currency(au)
    if cs and ca and cs != ca:  # converted between currencies: not comparable here
        return None
    a = src * scale(f"{su} {claim.get('source_value')}")  # «4573亿» may carry its unit inside the value
    b = art * scale(f"{au} {claim.get('article_value')}")
    if a == 0 or b == 0:
        return None if a == b else f"источник {claim.get('source_value')} {su}, в статье {claim.get('article_value')} {au}"
    if abs(a - b) / max(abs(a), abs(b)) <= SCALE_TOLERANCE:
        return None
    ratio = max(abs(a), abs(b)) / min(abs(a), abs(b))
    hint = f" (расхождение в {ratio:.0f} раз)" if ratio >= 5 else ""
    return f"источник {claim.get('source_value')} {su}, в статье {claim.get('article_value')} {au}{hint}"


def _resolve(report: dict, path: str):
    """'sections[3].body' → (container, key) so that container[key] is the text; None when the path is unknown."""
    node, parts = report, [p for p in str(path or "").split(".") if p]
    if not parts:
        return None
    for i, part in enumerate(parts):
        m = _PATH_RE.fullmatch(part)
        if not m:
            return None
        key, idx = m.group(1), m.group(2)
        last = i == len(parts) - 1
        if idx is None:
            if not isinstance(node, dict) or key not in node:
                return None
            if last:
                return (node, key) if isinstance(node[key], str) else None
            node = node[key]
        else:
            seq = node.get(key) if isinstance(node, dict) else None
            if not isinstance(seq, list) or not 0 <= int(idx) < len(seq):
                return None
            if last:
                return (seq, int(idx)) if isinstance(seq[int(idx)], str) else None
            node = seq[int(idx)]
    return None


def apply_fix(report: dict, claim: dict) -> bool:
    """Replace `fix.find` by `fix.replace` in the named field when it occurs there exactly once."""
    fix = claim.get("fix") or {}
    find, repl = str(fix.get("find") or ""), str(fix.get("replace") or "")
    target = _resolve(report, claim.get("field"))
    if not find or target is None or find == repl:
        return False
    box, key = target
    text = box[key]
    if text.count(find) != 1:
        return False
    box[key] = text.replace(find, repl)
    return True


def article_payload(report: dict) -> dict:
    """The final text exactly as the reader gets it, with field paths the checker can point to."""
    keep = ("headline", "teaser", "sections", "mechanism_chain", "timeline", "charts", "cis")
    out = {k: report.get(k) for k in keep if report.get(k)}
    out["images"] = [{"caption": m.get("caption", ""), "type": m.get("type", ""), "source": m.get("source_url", "")}
                     for m in report.get("images") or []]
    return out


def _normalise(claims) -> list[dict]:
    out = []
    for i, c in enumerate(claims if isinstance(claims, list) else []):
        if not isinstance(c, dict):
            continue
        status = str(c.get("status") or "").upper()
        c["status"] = status if status in STATUSES else "UNVERIFIED"
        c["id"] = str(c.get("id") or f"C{i + 1}")
        c["material"] = c.get("material") is not False
        out.append(c)
    return out


def _settle(report: dict, claims: list[dict], registry: SourceRegistry) -> list[dict]:
    """Server-side rules on top of the model's verdicts; returns the claims whose text was changed."""
    changed = []
    for c in claims:
        problem = scale_problem(c)
        if problem and c["status"] == "VERIFIED":
            c["status"], c["note"] = "ERROR", f"программная проверка масштаба: {problem}"
            c["programmatic"] = True
        if c["status"] == "VERIFIED" and c["material"] and not registry.get(str(c.get("source") or "")) \
                and not str(c.get("source_url") or "").startswith("http"):
            c["status"], c["note"] = "UNVERIFIED", "нет открытого источника"
        if c["status"] in ("ERROR", "UNVERIFIED") and c.get("fix"):
            if c["status"] == "ERROR" and not registry.get(str(c.get("source") or "")) \
                    and not str(c.get("source_url") or "").startswith("http"):
                continue  # a correction must cite the source that proves it
            if apply_fix(report, c):
                c["fixed"] = True
                changed.append(c)
    return changed


def summary(claims: list[dict]) -> dict:
    open_ = [c for c in claims if c.get("material") and c.get("status") != "VERIFIED"]
    return {"checked": len(claims), "verified": sum(c.get("status") == "VERIFIED" for c in claims),
            "errors_found": sum(bool(c.get("errors_found")) for c in claims),
            "fixed": sum(bool(c.get("fixed")) for c in claims),
            "open": [f"{c['id']}: {c.get('text', '')[:120]} — {c.get('status')} ({c.get('note', '')[:120]})" for c in open_]}


def run(app: App, report: dict, registry: SourceRegistry) -> dict:
    """FACT LEDGER of the final article. Writes report['fact_ledger'] and returns the summary.
    PeakHoursError propagates; any other failure means the gate did not pass (status BLOCKED, never READY)."""
    s, ds = app.settings, app.deepseek
    prompt = app.prompt("riddle_factcheck")
    if report.get("fact_ledger") and "author_fact_ledger" not in report:  # the writer's own ledger: claims, not proof
        report["author_fact_ledger"] = report.pop("fact_ledger")
    payload = {"company": report.get("company_name"), "article": article_payload(report),
               "author_ledger": report.get("author_fact_ledger") or (report.get("research_notes") or {}).get("financials") or [],
               "known_sources": registry.brief()}
    try:
        result = ds.research(s.deepseek_model_pro, prompt, payload, registry, max_tool_calls=MAX_TOOL_CALLS,
                             cache_ttl=CACHE_HOURS, max_tokens=32000)
    except PeakHoursError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("riddle fact check failed: %s", type(exc).__name__)
        report["fact_ledger"] = []
        return {"checked": 0, "verified": 0, "errors_found": 0, "fixed": 0,
                "open": [f"фактчек не выполнен: {type(exc).__name__}"]}
    claims = _normalise(result.get("claims"))
    for c in claims:
        c["errors_found"] = c["status"] == "ERROR" or bool(scale_problem(c))
    changed = _settle(report, claims, registry)
    if changed:  # §15 step 5: the corrected text is checked again
        recheck = [{k: c.get(k) for k in ("id", "field", "text", "kind", "source")} | {"fixed_to": c["fix"].get("replace")}
                   for c in changed]
        try:
            again = ds.research(s.deepseek_model_pro, prompt, {**payload, "article": article_payload(report),
                                                                "recheck": recheck}, registry,
                                max_tool_calls=MAX_RECHECK_CALLS, cache_ttl=CACHE_HOURS, max_tokens=16000)
        except PeakHoursError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("riddle fact recheck failed: %s", type(exc).__name__)
            again = {}
        by_id = {c["id"]: c for c in claims}
        rechecked = set()
        for c in _normalise(again.get("claims")):
            old = by_id.get(c["id"])
            if old is None or not old.get("fixed"):
                continue
            c["fixed"], c["errors_found"], c["fix"] = True, old.get("errors_found"), None
            problem = scale_problem(c)
            if problem and c["status"] == "VERIFIED":
                c["status"], c["note"] = "ERROR", f"программная проверка масштаба: {problem}"
            by_id[c["id"]] = c
            rechecked.add(c["id"])
        for cid, c in by_id.items():  # a fix that was not confirmed again stays open
            if c.get("fixed") and cid not in rechecked:
                c["status"], c["note"] = "UNVERIFIED", "исправление не прошло повторную проверку"
        claims = list(by_id.values())
    jev_note = judge_with_jev(app, claims, registry)
    report["fact_ledger"] = claims
    out = summary(claims)
    out["jev"] = jev_note
    if jev_note.get("error"):
        out["open"].append(f"проверка TypeSafe Jev не выполнена: {jev_note['error']}")
    log.info("riddle fact check: %s claims, %s verified, %s errors found, %s fixed, %s open",
             out["checked"], out["verified"], out["errors_found"], out["fixed"], len(out["open"]))
    return out


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def judge_with_jev(app: App, claims: list[dict], registry: SourceRegistry) -> dict:
    """Second independent check: every material VERIFIED claim with its source quote goes to TypeSafe Jev (choice
    supported / contradicted / insufficient with probabilities and confidence). Anything short of a confident
    «supported» is no longer VERIFIED. The quote must really be on the opened page."""
    s = app.settings
    jev = Jev(app.http, s.typesafe_api_key, s.typesafe_url, s.typesafe_model)
    todo = [c for c in claims if c.get("material") and c.get("status") == "VERIFIED"]
    if not todo:
        return {"checked": 0}
    if not jev.enabled:
        return {"checked": 0, "error": "нет TYPESAFE_API_KEY"}
    checked = failed = 0
    for c in todo:
        quote = str(c.get("source_quote") or "").strip()
        src = registry.get(str(c.get("source") or "")) or registry.get(str(c.get("source_url") or ""))
        if not quote:
            c["status"], c["note"] = "UNVERIFIED", "нет точной выдержки из источника для независимой проверки"
            failed += 1
            continue
        if src and src.content and _squash(quote) not in _squash(src.content):
            c["status"], c["note"] = "UNVERIFIED", "выдержки нет на открытой странице источника"
            failed += 1
            continue
        state = (f"CLAIM: {c.get('text', '')}\n"
                 f"VALUE IN ARTICLE: {c.get('article_value', '')} {c.get('article_unit', '')}\n"
                 f"SOURCE: {(src.url if src else c.get('source_url', ''))} (period {c.get('period', '')})\n"
                 f"SOURCE VALUE: {c.get('source_value', '')} {c.get('source_unit', '')}\n"
                 f"EXCERPT: {quote[:3000]}")
        try:
            answer = jev.choice(state, JEV_INSTRUCTIONS, JEV_CRITERIA, qid=c["id"])
        except Exception as exc:  # noqa: BLE001 - one failed call: the claim is not independently confirmed
            log.warning("jev check failed for %s: %s", c["id"], type(exc).__name__)
            c["status"], c["note"] = "UNVERIFIED", f"TypeSafe Jev не ответил ({type(exc).__name__})"
            failed += 1
            continue
        checked += 1
        probs = answer["probabilities"]
        c["jev"] = {"choice": answer["choice"], "supported": round(probs.get("supported", 0.0), 3),
                    "confidence": round(answer["confidence"], 3), "model": answer["model"]}
        if answer["choice"] == "supported" and probs.get("supported", 0.0) >= JEV_MIN_SUPPORT \
                and answer["confidence"] >= JEV_MIN_CONFIDENCE:
            continue
        failed += 1
        if probs.get("contradicted", 0.0) >= JEV_CONTRADICTED:
            c["status"], c["note"] = "ERROR", f"TypeSafe Jev: источник противоречит (p={probs.get('contradicted', 0):.2f})"
        else:
            c["status"], c["note"] = "UNVERIFIED", (f"TypeSafe Jev: не подтверждено уверенно (supported="
                                                    f"{probs.get('supported', 0):.2f}, confidence={answer['confidence']:.2f})")
    log.info("jev: %s claims judged, %s not confirmed", checked, failed)
    return {"checked": checked, "not_confirmed": failed}


# --- published page vs verified text (§19) -------------------------------------------------------

_NUM_RE = re.compile(r"\d+(?:[.,\s ]\d+)*")


def numbers(text: str) -> set[str]:
    """Normalised numbers of a text: '457,3' and '457.3' are the same; '1 999' is '1999'. Single digits are skipped
    (list numbering and the like are not facts)."""
    out = set()
    for m in _NUM_RE.findall(text or ""):
        n = re.sub(r"\s", "", m).replace(",", ".").rstrip(".")
        if len(n) > 1:
            out.add(n)
    return out


def page_text(nodes) -> str:
    parts: list[str] = []

    def walk(items):
        for n in items or []:
            if isinstance(n, str):
                parts.append(n)
            elif isinstance(n, dict):
                if n.get("tag") == "img":
                    continue
                walk(n.get("children"))
                if n.get("tag") in ("p", "h3", "h4", "li", "figcaption", "blockquote"):
                    parts.append("\n")
    walk(nodes)
    return "".join(parts)
