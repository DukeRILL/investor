"""Convert the model's light markup (**bold**, • bullets, [S1]) to Telegram HTML and Telegraph nodes."""

import re
from html import escape

from .sources import CITE_RE, SourceRegistry

TG_LIMIT = 4096
BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.S)


def _cite_html(text: str, registry: SourceRegistry | None) -> str:
    def repl(m: re.Match) -> str:
        src = registry.get(m.group(1)) if registry else None
        if not src:
            return ""
        return f'<a href="{escape(src.url, quote=True)}">[{escape(src.publisher or src.id)}]</a>'
    return CITE_RE.sub(repl, text)


def to_tg_html(text: str, registry: SourceRegistry | None = None) -> str:
    """Escape everything, then re-apply bold and source links."""
    html = escape(text or "", quote=False)
    html = BOLD_RE.sub(lambda m: f"<b>{m.group(1)}</b>", html)
    html = _cite_html(html, registry)
    html = re.sub(r"[ \t]+\n", "\n", html)
    return html.strip()


def visible_len(html: str) -> int:
    return len(re.sub(r"<[^>]+>", "", html))


def split_html(blocks: list[str], limit: int = TG_LIMIT - 20) -> list[str]:
    """Pack HTML blocks (paragraphs) into messages without cutting a block, then label parts."""
    parts: list[str] = []
    current = ""
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        pieces = [block] if visible_len(block) <= limit else _split_long(block, limit)
        for piece in pieces:
            candidate = f"{current}\n\n{piece}" if current else piece
            if visible_len(candidate) <= limit and len(candidate) <= TG_LIMIT * 2:
                current = candidate
            else:
                if current:
                    parts.append(current)
                current = piece
    if current:
        parts.append(current)
    if len(parts) > 1:
        parts = [f"{p}\n\n<i>{i}/{len(parts)}</i>" for i, p in enumerate(parts, 1)]
    return parts


def _split_long(block: str, limit: int) -> list[str]:
    out, cur = [], ""
    for line in block.split("\n"):
        cand = f"{cur}\n{line}" if cur else line
        if visible_len(cand) <= limit:
            cur = cand
        else:
            if cur:
                out.append(cur)
            cur = line[:limit]
    if cur:
        out.append(cur)
    return out


# --- Telegraph -------------------------------------------------------------------------------

def _inline_nodes(text: str, registry: SourceRegistry | None, cite: str = "publisher") -> list:
    nodes: list = []
    pos = 0
    pattern = re.compile(r"\*\*(.+?)\*\*|\[(S\d+)\]", re.S)
    for m in pattern.finditer(text):
        if m.start() > pos:
            nodes.append(text[pos:m.start()])
        if m.group(1) is not None:
            nodes.append({"tag": "b", "children": [m.group(1)]})
        else:
            src = registry.get(m.group(2)) if registry else None
            if src:
                label = src.id[1:] if cite == "number" else (src.publisher or src.id)
                nodes.append({"tag": "a", "attrs": {"href": src.url}, "children": [f"[{label}]"]})
        pos = m.end()
    if pos < len(text):
        nodes.append(text[pos:])
    return nodes


def text_to_nodes(text: str, registry: SourceRegistry | None = None, cite: str = "publisher") -> list:
    """Paragraphs -> <p>, '• ' lines -> <ul>, '1. ' lines -> <ol>, '### ' lines -> <h4>.
    cite='number' renders [S12] as a short numbered link [12] instead of the publisher name."""
    nodes: list = []
    for para in re.split(r"\n\s*\n", (text or "").strip()):
        lines = [ln.strip() for ln in para.split("\n") if ln.strip()]
        bullets: list = []
        numbered: list = []
        buffer: list[str] = []

        def flush_buffer():
            if buffer:
                nodes.append({"tag": "p", "children": _inline_nodes("\n".join(buffer), registry, cite)})
                buffer.clear()

        def flush_lists():
            if bullets:
                nodes.append({"tag": "ul", "children": list(bullets)})
                bullets.clear()
            if numbered:
                nodes.append({"tag": "ol", "children": list(numbered)})
                numbered.clear()

        for line in lines:
            ordered = re.match(r"^\d{1,2}[.)]\s+(.*)", line)
            if line.startswith("### "):
                flush_buffer()
                flush_lists()
                nodes.append({"tag": "h4", "children": _inline_nodes(line[4:], registry, cite)})
            elif line.startswith(("• ", "- ", "* ")):
                flush_buffer()
                if numbered:
                    flush_lists()
                bullets.append({"tag": "li", "children": _inline_nodes(line[2:], registry, cite)})
            elif ordered:
                flush_buffer()
                if bullets:
                    flush_lists()
                numbered.append({"tag": "li", "children": _inline_nodes(ordered.group(1), registry, cite)})
            else:
                flush_lists()
                buffer.append(line)
        flush_buffer()
        flush_lists()
    return nodes


def table_nodes(headers: list[str], rows: list[list[str]]) -> list:
    """Telegraph has no tables: render each row as a compact bullet list."""
    items = []
    for row in rows:
        cells = [f"{h}: {c}" for h, c in zip(headers[1:], row[1:]) if c not in (None, "")]
        items.append({"tag": "li", "children": [{"tag": "b", "children": [str(row[0])]}, " — " + "; ".join(cells)]})
    return [{"tag": "ul", "children": items}] if items else []
