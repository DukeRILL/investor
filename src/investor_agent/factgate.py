"""Fact-check gate shared by «Разгадка бизнеса» and «Школа единорогов» (owner, 2026-11-09; docs/RULES.md §5).

1. DeepSeek, as an independent checker, extracts the material claims of the FINAL article into a FACT LEDGER
   (VERIFIED / UNVERIFIED / ERROR, source value + unit, article value + unit, exact source quote).
2. The server recomputes every unit conversion itself (亿 / млн / млрд …): a scale mismatch is an ERROR whatever the
   model says; the quote must really be on the opened source page.
3. TypeSafe Jev judges every material VERIFIED claim against its quote (choice with probabilities and confidence).
4. Whatever is still wrong goes back to DeepSeek to correct (with a source), soften or remove; the corrected claims
   are checked again by both. Up to `rounds` times.
The gate passes only when no material claim is left open. `strict` (Разгадка) checks everything with tight
tolerances; `approx` (Школа) checks only the important claims and accepts approximate figures.
"""

import logging
import re

from dataclasses import dataclass
from typing import Callable

from .app import App
from .sources import SourceRegistry
from .timeutil import PeakHoursError
from .typesafe import Jev

log = logging.getLogger(__name__)

STATUSES = ("VERIFIED", "UNVERIFIED", "ERROR")
INVARIANT_FIELDS = ("id", "field", "text", "kind", "material")   # a recheck can never change what was claimed or how important it is
RECHECK_FIELDS = ("status", "note", "source", "source_url", "source_quote", "source_value", "source_unit",
                  "article_value", "article_unit", "period", "fix")
CACHE_HOURS = 24          # the pages were read during research: re-reading them is free
MAX_RECHECK_CALLS = 20
PERCENT_TOLERANCE = 0.15  # percentage points
# TypeSafe Jev, the second independent judge (owner, 2026-11-09 15:48 UTC): a VERIFIED claim stays VERIFIED only when
# Jev says the source quote supports it with high probability and confidence.
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


@dataclass(frozen=True)
class Mode:
    name: str
    scale_tolerance: float     # 457.3 vs 457 is rounding; a tenfold error is never within it
    jev_min_support: float
    jev_min_confidence: float
    rounds: int                # how many times open claims go back to DeepSeek
    max_claims: int            # «approx»: only the most important claims are checked
    max_tool_calls: int
    pct_tolerance: float = PERCENT_TOLERANCE   # percentage points


STRICT = Mode("strict", 0.03, 0.80, 0.60, 2, 120, 40)                # «Разгадка бизнеса»: everything material, tight
APPROX = Mode("approx", 0.10, 0.50, 0.30, 2, 20, 20, 1.0)            # «Школа единорогов»: the important numbers, approximate

_PATH_RE = re.compile(r"([a-z_]+)(?:\[(\d+)\])?")


class NumberError(ValueError):
    """A string that looks like a number but cannot be read unambiguously («1,999», «1,2,3»). Never swallowed:
    the claim is blocked instead of being compared on a guess."""


_TOKEN_RE = re.compile(r"[-+]?\d[\d.,]*")


def _locale_free(token: str) -> bool:
    """«1,999» / «1.999»: one separator followed by exactly three digits after a 1–3 digit head: thousands (1999) or a
    decimal (1.999). Only the counterpart or the context can tell."""
    m = re.fullmatch(r"[-+]?(\d{1,3})([.,])(\d{3})", token)
    return bool(m) and not token.lstrip("+-").startswith("0")


def number(value, side: str | None = None) -> float | None:
    """'4.573,0' / '4,573.0' / '1 999' / '1,234,567' / '457,3' / 369 → float. None only when there is no digit at all.
    Raises NumberError for a digit string that cannot be read safely. A single separator followed by exactly three
    digits is ambiguous («1,999»). The locale is decided explicitly by the side of the ledger it comes from:
    `side="source"` (English / Chinese sources): «,» groups thousands, «.» is decimal → 1,999 = 1999, 1.999 = 1.999;
    `side="article"` (Russian text): «,» and «.» are decimal (thousands are written with a space) → 1,999 = 1.999.
    Without a side the ambiguous token raises."""
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"[\s\u00a0\u202f']", "", str(value or ""))
    m = _TOKEN_RE.search(text)
    if not m:
        return None
    raw = m.group(0).rstrip(".,")
    sign = -1.0 if raw.startswith("-") else 1.0
    raw = raw.lstrip("+-")
    dots, commas = raw.count("."), raw.count(",")
    if dots and commas:  # the LAST separator is the decimal one, the other kind must group by threes
        dec = "." if raw.rfind(".") > raw.rfind(",") else ","
        grp = "," if dec == "." else "."
        whole, _, frac = raw.rpartition(dec)
        if dec in whole or not re.fullmatch(r"\d{1,3}(?:%s\d{3})+" % re.escape(grp), whole) or not frac.isdigit():
            raise NumberError(f"неоднозначная запись числа: {value!r}")
        return sign * float(whole.replace(grp, "") + "." + frac)
    sep = "." if dots else "," if commas else ""
    if not sep:
        return sign * float(raw)
    parts = raw.split(sep)
    if len(parts) > 2:  # 1,234,567 / 1.234.567: groups of exactly three after a 1–3 digit head
        if re.fullmatch(r"\d{1,3}(?:%s\d{3})+" % re.escape(sep), raw) and not raw.startswith("0"):
            return sign * float(raw.replace(sep, ""))
        raise NumberError(f"неоднозначная запись числа: {value!r}")
    if _locale_free(raw):
        if side == "source":
            return sign * float(raw.replace(",", "") if sep == "," else raw)
        if side == "article":
            return sign * float(raw.replace(sep, "."))
        raise NumberError(f"неоднозначная запись числа: {value!r}")
    return sign * float(raw.replace(sep, "."))


def scale(unit: str) -> float:
    u = str(unit or "").lower()
    for marker, factor in _SCALES:
        if marker.isascii():
            if re.search(r"(?<![a-z])%s(?![a-z])" % re.escape(marker), u):
                return factor
        elif marker in u:
            return factor
    return 1.0


def currency(unit: str) -> str | None:
    u = str(unit or "").lower()
    for code, markers in _CURRENCIES:
        if any(m in u for m in markers):
            return code
    return None


def _is_percent(unit: str, value) -> bool:
    u = f"{unit} {value}".lower()
    return "%" in u or "процент" in u or "percent" in u


def scale_problem(claim: dict, tolerance: float = 0.03, pct_tolerance: float = PERCENT_TOLERANCE) -> str | None:
    """Programmatic check of the conversion source → article (§16). None = consistent or not comparable (no numbers
    at all). A digit string that cannot be read safely is a PROBLEM, never silently skipped."""
    sv, av = claim.get("source_value"), claim.get("article_value")
    su, au = str(claim.get("source_unit") or ""), str(claim.get("article_unit") or "")
    try:
        src, art = number(sv, "source"), number(av, "article")
        if src is not None and art is not None and re.sub(r"\s", "", str(sv)) == re.sub(r"\s", "", str(av)):
            art = src  # the same printed number on both sides is the same number (only the unit can differ)
    except NumberError as exc:
        return str(exc)
    if src is None or art is None:
        return None
    pct_s, pct_a = _is_percent(su, sv), _is_percent(au, av)
    if pct_s or pct_a:
        if pct_s != pct_a:  # 0.76 (a ratio) against 76 % or the same number with the unit missing on one side
            p, q = (src, art) if pct_s else (art, src)
            if min(abs(q * 100 - p), abs(q - p)) <= pct_tolerance:
                return None
            return f"доля/процент: источник {sv} {su}, в статье {av} {au}"
        return None if abs(src - art) <= pct_tolerance else f"источник {src}%, в статье {art}%"
    cs, ca = currency(su), currency(au)
    if cs and ca and cs != ca:  # converted between currencies: not comparable here
        return None
    a = src * scale(f"{su} {sv}")  # «4573亿» may carry its unit inside the value
    b = art * scale(f"{au} {av}")
    if a == 0 or b == 0:
        return None if a == b else f"источник {sv} {su}, в статье {av} {au}"
    if abs(a - b) / max(abs(a), abs(b)) <= tolerance:
        return None
    ratio = max(abs(a), abs(b)) / min(abs(a), abs(b))
    hint = f" (расхождение в {ratio:.0f} раз)" if ratio >= 5 else ""
    return f"источник {sv} {su}, в статье {av} {au}{hint}"


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


def _source(c: dict, registry: SourceRegistry):
    """The source a claim rests on, only if it is in the registry AND was really opened (fetch_page). A bare id from a
    search result, an id the model made up, or an arbitrary `source_url` is no evidence."""
    for ref in (c.get("source"), c.get("source_url")):
        src = registry.get(str(ref or "")) if ref else None
        if src and src.fetched:
            return src
    return None


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _text_values(text: str) -> set[float]:
    out = set()
    for n in numbers(text):
        try:
            out.add(float(n))
        except ValueError:
            pass
    return out


def _in_text(report: dict, c: dict) -> bool:
    """The article value the checker reports must really stand in the field it points to (no spoofed ledger).
    Tiny numbers (< 10) are not located reliably and are left to the arithmetic and the judge."""
    target = _resolve(report, c.get("field"))
    if target is None:
        return True
    try:
        wanted = number(c.get("article_value"), "article")
    except NumberError:
        return True  # scale_problem reports it
    if wanted is None or abs(wanted) < 10:
        return True
    box, key = target
    return wanted in _text_values(box[key])


def _settle(report: dict, claims: list[dict], registry: SourceRegistry, mode: Mode) -> None:
    """Server-side rules on top of the checker's verdicts. A claim whose text was corrected is open again until the
    next round confirms it with both checks."""
    for c in claims:
        if c.get("settled"):
            continue
        c["settled"] = True
        problem = scale_problem(c, mode.scale_tolerance, mode.pct_tolerance)
        if problem:
            c["errors_found"], c["material"] = True, True  # a numeric mismatch is never «not material»
            if c["status"] == "VERIFIED":
                c["status"], c["note"] = "ERROR", f"программная проверка: {problem}"
        if c["status"] == "ERROR":
            c["errors_found"] = True
        src = _source(c, registry)
        if c["status"] == "VERIFIED" and c["material"]:
            if src is None:
                c["status"], c["note"] = "UNVERIFIED", "источник не открывался (нет fetch_page)"
            elif not _in_text(report, c):
                c["status"], c["note"] = "UNVERIFIED", "число из реестра не найдено в указанном месте статьи"
        if c["status"] in ("ERROR", "UNVERIFIED") and c.get("fix"):
            fix = c["fix"] if isinstance(c["fix"], dict) else {}
            if str(fix.get("replace") or "").strip() and c["status"] == "ERROR" and src is None:
                continue  # a correction must cite a source that was really opened (a deletion needs none)
            if apply_fix(report, {**c, "fix": fix}):
                c["fixed"] = True
                if not str(fix.get("replace") or "").strip():  # the claim was taken out of the text
                    c["status"], c["note"] = "REMOVED", "утверждение удалено из текста"
                else:
                    c["status"], c["note"] = "UNVERIFIED", "текст исправлен, ждёт повторной проверки"


def judge_with_jev(app: App, claims: list[dict], registry: SourceRegistry, mode: Mode = STRICT) -> dict:
    """Every material VERIFIED claim not judged yet goes to TypeSafe Jev with its exact source quote (choice
    supported / contradicted / insufficient). Anything short of «supported» at the mode's thresholds is open again.
    The quote must really be on the opened page."""
    s = app.settings
    jev = Jev(app.http, s.typesafe_api_key, s.typesafe_url, s.typesafe_model)
    todo = [c for c in claims if c.get("material") and c.get("status") == "VERIFIED" and not c.get("jev")]
    if not todo:
        return {"checked": 0, "not_confirmed": 0}
    if not jev.enabled:
        return {"checked": 0, "not_confirmed": 0, "error": "нет TYPESAFE_API_KEY"}
    checked = failed = 0
    for c in todo:
        quote = str(c.get("source_quote") or "").strip()
        src = _source(c, registry)
        if src is None:
            c["status"], c["note"] = "UNVERIFIED", "источник не открывался (нет fetch_page)"
            failed += 1
            continue
        if not quote:
            c["status"], c["note"] = "UNVERIFIED", "нет точной выдержки из источника для независимой проверки"
            failed += 1
            continue
        if not src.content or _squash(quote) not in _squash(src.content):
            c["status"], c["note"] = "UNVERIFIED", "выдержки нет на открытой странице источника"
            failed += 1
            continue
        state = (f"CLAIM: {c.get('text', '')}\n"
                 f"VALUE IN ARTICLE: {c.get('article_value', '')} {c.get('article_unit', '')}\n"
                 f"SOURCE: {src.url} (period {c.get('period', '')})\n"
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
        if answer["choice"] == "supported" and probs.get("supported", 0.0) >= mode.jev_min_support \
                and answer["confidence"] >= mode.jev_min_confidence:
            continue
        failed += 1
        if probs.get("contradicted", 0.0) >= JEV_CONTRADICTED:
            c["status"], c["note"] = "ERROR", f"TypeSafe Jev: источник противоречит (p={probs.get('contradicted', 0):.2f})"
            c["errors_found"] = True
        else:
            c["status"], c["note"] = "UNVERIFIED", (f"TypeSafe Jev: не подтверждено уверенно (supported="
                                                    f"{probs.get('supported', 0):.2f}, confidence={answer['confidence']:.2f})")
    log.info("jev: %s claims judged, %s not confirmed", checked, failed)
    return {"checked": checked, "not_confirmed": failed}


def summary(claims: list[dict]) -> dict:
    open_ = _open(claims)
    return {"checked": len(claims), "verified": sum(c.get("status") == "VERIFIED" for c in claims),
            "errors_found": sum(bool(c.get("errors_found")) for c in claims),
            "fixed": sum(bool(c.get("fixed")) for c in claims),
            "removed": sum(c.get("status") == "REMOVED" for c in claims),
            "open": [f"{c['id']}: {c.get('text', '')[:120]} — {c.get('status')} ({c.get('note', '')[:120]})" for c in open_]}


def _open(claims: list[dict]) -> list[dict]:
    return [c for c in claims if c.get("material") and c.get("status") not in ("VERIFIED", "REMOVED")]


def run(app: App, report: dict, registry: SourceRegistry, article: Callable[[dict], dict], mode: Mode = STRICT,
        label: str = "") -> dict:
    """FACT LEDGER of the final article: DeepSeek check → server arithmetic → TypeSafe Jev → open claims back to
    DeepSeek (correct / soften / remove) → both checks again, up to `mode.rounds` times. Writes report['fact_ledger']
    and returns the summary ({"open": [...]} empty = passed). PeakHoursError propagates; any other failure leaves
    the gate closed (never passed by default)."""
    s, ds = app.settings, app.deepseek
    prompt = app.prompt("fact_gate")
    if report.get("fact_ledger") and "author_fact_ledger" not in report:  # the writer's own ledger: claims, not proof
        report["author_fact_ledger"] = report.pop("fact_ledger")
    payload = {"company": report.get("company_name"), "mode": mode.name, "max_claims": mode.max_claims,
               "article": article(report),
               "author_ledger": report.get("author_fact_ledger") or (report.get("research_notes") or {}).get("financials") or [],
               "known_sources": registry.brief()}
    try:
        result = ds.research(s.deepseek_model_pro, prompt, payload, registry, max_tool_calls=mode.max_tool_calls,
                             cache_ttl=CACHE_HOURS, max_tokens=32000)
    except PeakHoursError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("%s fact check failed: %s", label, type(exc).__name__)
        report["fact_ledger"] = []
        return {"checked": 0, "verified": 0, "errors_found": 0, "fixed": 0, "rounds": 0,
                "open": [f"фактчек не выполнен: {type(exc).__name__}"]}
    by_id = {c["id"]: c for c in _normalise(result.get("claims"))}
    jev_note: dict = {}
    rounds = 0
    while True:
        claims = list(by_id.values())
        _settle(report, claims, registry, mode)
        note = judge_with_jev(app, claims, registry, mode)
        if note.get("error"):
            jev_note = note
        else:
            jev_note = {"checked": jev_note.get("checked", 0) + note["checked"],
                        "not_confirmed": jev_note.get("not_confirmed", 0) + note["not_confirmed"]}
        left = _open(claims)
        if not left or rounds >= mode.rounds or jev_note.get("error"):
            break
        rounds += 1
        rework = [{k: c.get(k) for k in ("id", "field", "text", "kind", "source", "source_value", "source_unit",
                                         "article_value", "article_unit", "status", "note", "jev")} for c in left]
        try:
            again = ds.research(s.deepseek_model_pro, prompt, {**payload, "article": article(report),
                                                                "recheck": rework}, registry,
                                max_tool_calls=MAX_RECHECK_CALLS, cache_ttl=CACHE_HOURS, max_tokens=16000)
        except PeakHoursError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("%s fact recheck failed: %s", label, type(exc).__name__)
            break
        for new in _normalise(again.get("claims")):
            old = by_id.get(new["id"])
            if old is None or old.get("status") in ("VERIFIED", "REMOVED") or not old.get("material"):
                continue  # unknown ids are ignored; an open claim only changes through the server rules below
            merged = {k: old[k] for k in INVARIANT_FIELDS if k in old}  # id, field, text, kind, material stay the server's
            merged.update({k: new[k] for k in RECHECK_FIELDS if k in new})
            merged["errors_found"], merged["fixed"] = old.get("errors_found", False), old.get("fixed", False)
            by_id[merged["id"]] = merged  # no `settled`, no `jev`: arithmetic, source rule and Jev run again
    claims = list(by_id.values())
    for c in claims:
        c.pop("settled", None)
    report["fact_ledger"] = claims
    out = summary(claims)
    out["jev"], out["rounds"] = jev_note, rounds
    if jev_note.get("error"):
        out["open"].append(f"проверка TypeSafe Jev не выполнена: {jev_note['error']}")
    log.info("%s fact check: %s claims, %s verified, %s errors found, %s fixed, %s open after %s rounds", label,
             out["checked"], out["verified"], out["errors_found"], out["fixed"], len(out["open"]), rounds)
    return out


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
