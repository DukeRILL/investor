"""Registry of real sources seen by the agent. The model may cite only these ids."""

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

CITE_RE = re.compile(r"\[(S\d+)\]")

SYNDICATION_HOSTS = {"finance.yahoo.com", "news.yahoo.com", "msn.com", "www.msn.com", "marketscreener.com",
                     "www.marketscreener.com", "investing.com", "www.investing.com", "tradingview.com", "www.tradingview.com"}


# Owner's rule (docs/RULES.md §5): published links go only to news outlets and posts of large investors or
# platforms. Data portals, statistics and encyclopedias may be used for research but are never linked.
NON_CITABLE_HOSTS = {"fred.stlouisfed.org", "stlouisfed.org", "alfred.stlouisfed.org", "research.stlouisfed.org",
                     "wikipedia.org", "wikiwand.com", "wikidata.org", "statista.com", "tradingeconomics.com",
                     "macrotrends.net", "data.worldbank.org", "ycharts.com", "fxempire.com"}
MAX_LINKS_PER_PARAGRAPH = 3


def is_citable(url: str) -> bool:
    host = domain(url or "")
    if not host:
        return False
    return not any(host == h or host.endswith("." + h) for h in NON_CITABLE_HOSTS)


def domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


@dataclass
class Source:
    id: str
    url: str
    title: str = ""
    publisher: str = ""
    date: str | None = None
    summary: str = ""
    content: str = ""
    fetched: bool = False

    @property
    def domain(self) -> str:
        return domain(self.url)

    @property
    def citable(self) -> bool:
        return is_citable(self.url)


@dataclass
class SourceRegistry:
    sources: dict[str, Source] = field(default_factory=dict)
    _by_url: dict[str, str] = field(default_factory=dict)

    def add(self, url: str, title: str = "", publisher: str = "", date: str | None = None, summary: str = "") -> Source:
        url = url.strip()
        if url in self._by_url:
            src = self.sources[self._by_url[url]]
            src.title = src.title or title
            src.summary = src.summary or summary
            src.date = src.date or date
            return src
        sid = f"S{len(self.sources) + 1}"
        src = Source(id=sid, url=url, title=title, publisher=publisher or domain(url), date=date, summary=summary)
        self.sources[sid] = src
        self._by_url[url] = sid
        return src

    def get(self, ref: str) -> Source | None:
        if ref in self.sources:
            return self.sources[ref]
        sid = self._by_url.get(ref.strip())
        return self.sources.get(sid) if sid else None

    def mark_fetched(self, ref: str, title: str, content: str) -> Source | None:
        src = self.get(ref)
        if src:
            src.fetched = True
            src.title = src.title or title
            src.content = content
        return src

    def cited_ids(self, *texts: str) -> list[str]:
        seen: list[str] = []
        for text in texts:
            for sid in CITE_RE.findall(text or ""):
                if sid not in seen:
                    seen.append(sid)
        return seen

    def unknown_citations(self, *texts: str) -> list[str]:
        return [sid for sid in self.cited_ids(*texts) if sid not in self.sources]

    def verified(self, ids: list[str]) -> list[Source]:
        return [self.sources[i] for i in ids if i in self.sources and self.sources[i].fetched]

    def independent_publishers(self, ids: list[str]) -> int:
        """Count distinct publishers among fetched sources, collapsing syndication copies by title."""
        doms, titles = set(), set()
        for src in self.verified(ids):
            norm_title = re.sub(r"\W+", " ", src.title.lower()).strip()[:80]
            if src.domain in SYNDICATION_HOSTS or (norm_title and norm_title in titles):
                continue
            doms.add(src.domain)
            if norm_title:
                titles.add(norm_title)
        return len(doms)

    def brief(self, ids: list[str] | None = None, content_chars: int = 0) -> list[dict]:
        items = self.sources.values() if ids is None else [self.sources[i] for i in ids if i in self.sources]
        out = []
        for s in items:
            item = {"id": s.id, "url": s.url, "title": s.title, "publisher": s.publisher, "date": s.date,
                    "fetched": s.fetched, "summary": s.summary[:600]}
            if content_chars and s.content:
                item["content"] = s.content[:content_chars]
            out.append(item)
        return out


def cap_citations(text: str, registry: "SourceRegistry | None", limit: int = MAX_LINKS_PER_PARAGRAPH) -> str:
    """Per paragraph keep at most `limit` distinct citable [Sn] marks; drop the rest and non-citable ones."""
    def one(para: str) -> str:
        kept: list[str] = []

        def repl(m: re.Match) -> str:
            src = registry.get(m.group(1)) if registry else None
            if not src or not src.citable:
                return ""
            if m.group(1) in kept:
                return m.group(0)
            if len(kept) >= limit:
                return ""
            kept.append(m.group(1))
            return m.group(0)
        return re.sub(r"[ \t]+(?=[.,;:!?…)]|$)", "", CITE_RE.sub(repl, para), flags=re.M)
    return "".join(one(part) if i % 2 == 0 else part
                   for i, part in enumerate(re.split(r"(\n\s*\n)", text or "")))
