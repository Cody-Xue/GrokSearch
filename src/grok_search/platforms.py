"""web_search(platform=...) for the extra-source engines.

Grok only gets the platform as a "focus on" hint. The independent engines are steered harder: a known
platform name (or a hostname) becomes a domain list that is sent to Tavily as include_domains and to
Firecrawl as "site:" operators, and every hit is checked against it (plus the platform's short-link and
content hosts) by host before selection. A platform that cannot be mapped to domains is added to the
engine query as text instead, without a domain filter.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

# (label, aliases, domains sent to the engines). Aliases are compared after _normalize(): case-folded,
# without spaces, ".", "-", "_" and "·". A domain also covers its subdomains (www.github.com,
# gist.github.com, en.wikipedia.org).
_PLATFORMS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("GitHub", ("github",), ("github.com",)),
    ("GitLab", ("gitlab",), ("gitlab.com",)),
    ("Gitee", ("gitee", "码云"), ("gitee.com",)),
    ("Reddit", ("reddit",), ("reddit.com",)),
    ("X/Twitter", ("x", "twitter", "推特"), ("x.com", "twitter.com")),
    ("Stack Overflow", ("stackoverflow",), ("stackoverflow.com",)),
    ("Stack Exchange", ("stackexchange",), ("stackexchange.com",)),
    ("Hacker News", ("hackernews", "hn", "ycombinator"), ("news.ycombinator.com",)),
    ("YouTube", ("youtube", "油管"), ("youtube.com",)),
    ("arXiv", ("arxiv",), ("arxiv.org",)),
    ("Wikipedia", ("wikipedia", "维基百科", "維基百科"), ("wikipedia.org",)),
    ("知乎", ("知乎", "zhihu"), ("zhihu.com",)),
    ("Bilibili", ("bilibili", "b站", "哔哩哔哩", "嗶哩嗶哩"), ("bilibili.com",)),
    ("微博", ("微博", "weibo", "新浪微博"), ("weibo.com",)),
    ("CSDN", ("csdn",), ("csdn.net",)),
    ("掘金", ("掘金", "juejin"), ("juejin.cn",)),
    ("V2EX", ("v2ex",), ("v2ex.com",)),
    ("Hugging Face", ("huggingface", "hf", "抱抱脸"), ("huggingface.co",)),
    ("PyPI", ("pypi",), ("pypi.org",)),
    ("npm", ("npm", "npmjs"), ("npmjs.com",)),
    ("crates.io", ("crates", "cratesio"), ("crates.io",)),
    ("Docker Hub", ("dockerhub",), ("hub.docker.com",)),
    ("Medium", ("medium",), ("medium.com",)),
    ("Dev.to", ("devto",), ("dev.to",)),
    ("Quora", ("quora",), ("quora.com",)),
    ("LinkedIn", ("linkedin", "领英"), ("linkedin.com",)),
    ("Facebook", ("facebook",), ("facebook.com",)),
    ("Instagram", ("instagram",), ("instagram.com",)),
    ("TikTok", ("tiktok",), ("tiktok.com",)),
    ("抖音", ("抖音", "douyin"), ("douyin.com",)),
    ("小红书", ("小红书", "xiaohongshu", "rednote"), ("xiaohongshu.com",)),
    ("豆瓣", ("豆瓣", "douban"), ("douban.com",)),
    ("微信公众号", ("微信公众号", "公众号", "微信", "wechat", "weixin"), ("mp.weixin.qq.com",)),
    ("博客园", ("博客园", "cnblogs"), ("cnblogs.com",)),
    ("SegmentFault", ("segmentfault", "思否"), ("segmentfault.com",)),
    ("Product Hunt", ("producthunt",), ("producthunt.com",)),
    ("Semantic Scholar", ("semanticscholar",), ("semanticscholar.org",)),
    ("OpenReview", ("openreview",), ("openreview.net",)),
    ("PubMed", ("pubmed",), ("pubmed.ncbi.nlm.nih.gov",)),
)

_ALIASES: dict[str, tuple[str, ...]] = {alias: domains for _, aliases, domains in _PLATFORMS for alias in aliases}
# Short-link and content hosts of a platform: accepted by the result filter but not sent to the engines.
# A rarely indexed "site:" alternative makes Firecrawl's search drop the restriction: in a live test
# "site:github.com OR site:githubusercontent.com" returned 3 of 5 hits off GitHub, "site:github.com" alone
# none, for the same query.
_ALSO_ACCEPTED: dict[str, tuple[str, ...]] = {
    "github.com": ("githubusercontent.com",),
    "reddit.com": ("redd.it",),
    "youtube.com": ("youtu.be",),
    "bilibili.com": ("b23.tv",),
    "weibo.com": ("weibo.cn",),
    "huggingface.co": ("hf.co",),
}
# Inside a longer phrase ("GitHub issues") only aliases this long count, so "X Window System" never turns
# into an X/Twitter filter by accident.
_MIN_PHRASE_ALIAS = 3

# Several platforms: "GitHub, Reddit", "知乎、B站", "GitHub | Reddit".
_HARD_SEPARATORS = re.compile(r"[,，、;；|｜\n]+")
# Inside one part: "X/Twitter", "GitHub & Reddit", "知乎和B站", "GitHub or Reddit", "GitHub issues".
_SOFT_SEPARATORS = re.compile(r"\s*(?:/|&|＆|和|或|与|及)\s*|\s+(?:and|or)\s+|\s+", re.IGNORECASE)
_NORMALIZE_RE = re.compile(r"[\s._\-·]+")
_HOST_RE = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]")
# "Node.js", "Next.js", "discord.py": project names written like hostnames (file extensions that are not
# TLDs, or are, like .py, but name libraries far more often than sites). A domain filter on such a name
# makes the engines return nothing ("discord.py" does not resolve at all).
_NOT_TLDS = {"js", "ts", "jsx", "tsx", "mjs", "cjs", "py", "pyi", "rb", "php", "java", "kt", "swift", "lua",
             "c", "h", "cpp", "hpp", "cs", "vue", "css", "scss"}
_MAX_REQUESTED_CHARS = 200


def _normalize(name: str) -> str:
    return _NORMALIZE_RE.sub("", name.casefold())


def _hostname(text: str) -> str:
    """'github.com', 'https://docs.python.org/3/' or 'www.v2ex.com' -> the host without 'www.'; else ''.

    A bare name whose last part is written in capitals ("ASP.NET", "Socket.IO") is a product name, not
    a hostname: people type hosts in lower case. With a scheme, a path or "www." it is a host anyway."""
    text = text.strip()
    if not text or any(ch.isspace() for ch in text):
        return ""
    try:
        parsed = urlparse(text if "://" in text else "//" + text)
        host = parsed.hostname or ""
    except ValueError:
        return ""
    bare = "://" not in text and not parsed.path.strip("/") and not text.lower().startswith("www.")
    if bare and any(ch.isupper() for ch in text.rstrip(".").rsplit(".", 1)[-1]):
        return ""
    host = host.rstrip(".").lower()
    if host.startswith("www."):
        host = host[4:]
    if "." not in host or not _HOST_RE.fullmatch(host) or host.rsplit(".", 1)[1] in _NOT_TLDS:
        return ""
    return host


def _resolve_name(text: str, *, min_alias: int = 1) -> tuple[str, ...] | None:
    key = _normalize(text)
    if key in _ALIASES and len(key) >= min_alias:
        return _ALIASES[key]
    host = _hostname(text)
    return (host,) if host else None


def host_matches(url: str, domains) -> bool:
    """Host-suffix match: the domain itself or a subdomain of it, never a substring (githubhelp.com is not GitHub)."""
    try:
        host = (urlparse((url or "").strip()).hostname or "").rstrip(".").lower()
    except ValueError:
        return False
    if host.startswith("www."):
        host = host[4:]
    return bool(host) and any(host == d or host.endswith("." + d) for d in domains)


@dataclass
class PlatformScope:
    """How one web_search platform value is applied to the extra-source engines."""

    requested: str
    domains: list[str] = field(default_factory=list)  # sent to the engines (empty: no domain filter)
    hint: str = ""  # text appended to the engine query
    unresolved: list[str] = field(default_factory=list)

    @property
    def also_accepted(self) -> list[str]:
        extra: list[str] = []
        for domain in self.domains:
            extra.extend(d for d in _ALSO_ACCEPTED.get(domain, ()) if d not in self.domains and d not in extra)
        return extra

    @property
    def applied(self) -> str:
        if self.domains:
            return "domain_filter"
        return "query_hint" if self.hint else "none"

    def engine_query(self, query: str) -> str:
        return f"{query} {self.hint}" if self.hint else query

    def allows(self, url: str) -> bool:
        return not self.domains or host_matches(url, self.domains + self.also_accepted)

    def report(self, engines, off_platform_skipped: int) -> dict:
        out: dict = {"requested": self.requested, "applied": self.applied}
        if self.domains:
            out["domains"] = list(self.domains)
            if self.also_accepted:
                out["also_accepted"] = self.also_accepted
            methods = {"tavily": "include_domains", "firecrawl": "site:"}
            out["engines"] = {engine: methods.get(engine, "filter") for engine in engines}
            out["off_platform_skipped"] = off_platform_skipped
        elif self.hint:
            out["engines"] = {engine: "query" for engine in engines}
        if self.hint:
            out["query_hint"] = self.hint
        if self.unresolved:
            out["unresolved"] = list(self.unresolved)
        return out


def _match_names(pieces: list[str], min_alias: int) -> tuple[list[str], list[str]]:
    """Greedy longest match of up to three adjacent words ("Stack Overflow", "Hacker News")."""
    found: list[str] = []
    words: list[str] = []
    i = 0
    while i < len(pieces):
        for n in range(min(3, len(pieces) - i), 0, -1):
            hit = _resolve_name(" ".join(pieces[i:i + n]), min_alias=min_alias)
            if hit:
                found.extend(hit)
                i += n
                break
        else:
            words.append(pieces[i])
            i += 1
    return found, words


def resolve_platform(platform: str) -> PlatformScope:
    """Map a platform value to domains.

    Each part (split on , ， 、 ; ； |) must be a known platform name or a hostname. Inside a part,
    "X/Twitter", "知乎和B站" or "GitHub Reddit" are several names; words next to a known name
    ("GitHub issues") are kept as query text. When any part names nothing known, no domain filter is
    applied (it would drop that platform's results) and the whole platform text goes into the engine query.
    """
    requested = " ".join((platform or "").split())[:_MAX_REQUESTED_CHARS]
    domains: list[str] = []
    extra_words: list[str] = []
    unresolved: list[str] = []
    for part in _HARD_SEPARATORS.split(requested):
        part = part.strip()
        if not part:
            continue
        found = list(_resolve_name(part) or ())
        words: list[str] = []
        if not found:
            pieces = [piece.strip() for piece in _SOFT_SEPARATORS.split(part) if piece and piece.strip()]
            if len(pieces) > 1:
                found, words = _match_names(pieces, 1)
                if words:
                    # Not every word is a platform: only longer names count next to ordinary words.
                    found, words = _match_names(pieces, _MIN_PHRASE_ALIAS)
        if not found:
            unresolved.append(part)
            continue
        domains.extend(d for d in found if d not in domains)
        extra_words.extend(w for w in words if w not in extra_words)
    if unresolved:
        return PlatformScope(requested, [], requested, unresolved)
    return PlatformScope(requested, domains, " ".join(extra_words))
