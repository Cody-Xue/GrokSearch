"""One detector for login walls and anti-bot interstitials, shared by web_fetch and URL verification.

Bot checks usually answer HTTP 200 (Reddit's "Prove your humanity", PyPI's Fastly "Client Challenge",
Baidu's "百度安全验证", Cloudflare's "Just a moment..."), so the status code alone does not tell them
apart from the requested page. Three signals do:

* the title, matched as a whole and on each side of a " - " / " | " / "_" site separator
  ("Reddit - Prove your humanity", "安全验证 - 知乎");
* challenge markup in the raw page (Reddit's hidden js_challenge form, Fastly's /_fs-ch- assets,
  Cloudflare's challenge page script) or challenge wording near the top of the visible text;
* a redirect to a sign-in page (only when the caller says the request was redirected): a sign-in
  title or first heading on a short page or one with a password field, or a final URL whose host,
  query or last path segment names a login wall that the requested URL did not already name. A wall
  segment elsewhere in the path counts when the redirect also left the requested host
  (drive.google.com -> accounts.google.com/v3/signin/identifier); auth0.com/docs/login moving to
  /docs/authenticate/login on the same host is documentation, not a wall.

Articles about captchas ("CAPTCHA - Wikipedia", "如何实现短信验证码登录 - 知乎") must not be flagged:
a title match alone counts only for a short page (at most WALL_MAX_CHARS characters of visible text,
GENERIC_MAX_CHARS when the only match is the bare word "captcha" / "验证码"), and challenge markup or
wording without a matching title only counts on a short page as well. A body that was cut off (the
caller read only part of it, or it is longer than BODY_SCAN_CHARS) is never short: the first 64 KB of
a 300 KB GitHub issue page hold under 1,000 visible characters.
"""
import html
import re
from urllib.parse import urlsplit

WALL_MAX_CHARS = 1500
GENERIC_MAX_CHARS = 400
BODY_SCAN_CHARS = 65536  # Reddit's challenge form sits behind an 8 KB inline SVG
WORDING_SCAN_CHARS = 2000  # of visible text: challenge pages say what they are at the top

WALL_TITLE_RE = re.compile(
    r"(?:百度)?安全验证|\S{0,4}验证码\S{0,2}|人机(?:身份)?验证|环境异常|访问异常|访问验证|滑动验证|unhuman|captcha|"
    r"just a moment\W*|attention required\W*|one more step\W*|are you a (?:robot|human)\W*|"
    r"(?:please )?verify(?:ing)? (?:that )?you(?: are|'re) (?:a )?human\W*|prove your humanity\W*|"
    r"client challenge|human verification|bot verification|security verification|robot check|"
    r"pardon our interruption\W*|access denied\W*|security check\W*|ddos-guard",
    re.I)
# Title matches that are also ordinary words in article titles; they need a very short page.
_GENERIC_TITLE_RE = re.compile(r"captcha|\S{0,4}验证码\S{0,2}", re.I)
TITLE_SEPARATOR_RE = re.compile(r"\s+[-|–—·]\s+|_")
# "Sign in", "Sign-in", "Sign in to GitHub", "Log in or sign up", "用户登录" -- not "Sign in with Apple"
# or "Login Authentication on Auth0", which are documentation about signing in.
LOGIN_TITLE_RE = re.compile(
    r"(?:log[ -]?in|sign[ -]?in)(?:\s+(?:to|into)\s.*|\s+(?:or|and|&)\s+(?:sign[ -]?up|register|join|create)\b.*|"
    r"\s+(?:required|page|here|now)\b)?\W*|\S{0,6}(?:登录|登入)\S{0,4}",
    re.I)
# Product prefixes of sign-in titles: "Google Drive: Sign-in".
_LOGIN_TITLE_PREFIX_RE = re.compile(r"[:：]\s*")
_WALL_SEGMENTS = {"login", "log-in", "signin", "sign-in", "sign_in", "logon", "servicelogin", "unhuman", "captcha", "authwall"}
_WALL_HOST_LABELS = {"login", "signin", "passport", "sso"}
_WALL_QUERY_RE = re.compile(r"(?:^|&)(?:need_login|captcha)=", re.I)

# Markup only a challenge page carries (searched in the raw text). Cloudflare also injects
# /cdn-cgi/challenge-platform/scripts/jsd/ into ordinary pages, so only the challenge page's own
# paths count.
CHALLENGE_MARKUP_RE = re.compile(
    r"cf-challenge|_cf_chl_opt|/cdn-cgi/challenge-platform/h/|name=[\"']?js_challenge\b|name=[\"']?jsc_token\b|"
    r"/_fs-ch-|captcha-delivery\.com|id=[\"']?px-captcha\b",
    re.I)
# Wording of challenge pages (searched in the first WORDING_SCAN_CHARS of visible text).
CHALLENGE_WORDING_RE = re.compile(
    r"checking (?:if the site connection is secure|your browser)|verifying you are human|"
    r"needs to review the security of your connection|enable javascript and cookies to continue|"
    r"prove your humanity|let us know you(?:'|’)re a real person|please complete the security check|"
    r"incapsula incident id|"
    r"网络不给力，请稍后重试|请完成(?:下方|以下)?(?:安全)?验证|请进行(?:安全|人机)验证|拖动(?:下方)?滑块",
    re.I)

_HTML_HINT_RE = re.compile(r"<(?:!doctype|html|head|body|div|meta|script|title)\b", re.I)
_HIDDEN_OPEN_RE = re.compile(r"<(script|style|svg|noscript|template)\b", re.I)
_TAG_RE = re.compile(r"<[^<>]*>")  # [^<>]: linear even on "<<<<..." without any ">"
_PASSWORD_INPUT_RE = re.compile(r"<input\b[^<>]{0,500}?\btype\s*=\s*[\"']?password\b", re.I)
_MD_HEADING_RE = re.compile(r"^#{1,3}[ \t]+(.{1,200}?)[ \t#]*$", re.M)


def url_key(url: str) -> str:
    """URL identity for redirect detection: scheme, www., trailing slash and fragment ignored."""
    try:
        parts = urlsplit((url or "").strip())
        host = (parts.hostname or "").lower().removeprefix("www.")
        return f"{host}:{parts.port or ''}{parts.path.rstrip('/')}?{parts.query}"
    except ValueError:
        return (url or "").strip()


def visible_text(text: str) -> str:
    """The readable text of an HTML page (script/style/svg removed, tags stripped); other text as is.

    Linear in the input: every hidden block is skipped with one forward search for its end tag.
    """
    text = text or ""
    if not _HTML_HINT_RE.search(text[:4096]):
        return text
    lowered = text.lower()
    out = []
    pos = 0
    while True:
        m = _HIDDEN_OPEN_RE.search(text, pos)
        if not m:
            out.append(text[pos:])
            break
        out.append(text[pos:m.start()])
        end = lowered.find(f"</{m.group(1).lower()}", m.end())
        if end == -1:
            break  # unterminated: the rest is not visible
        close = text.find(">", end)
        pos = len(text) if close == -1 else close + 1
    return " ".join(html.unescape(_TAG_RE.sub(" ", "".join(out))).split())


def markdown_title(text: str) -> str:
    """First Markdown heading near the top (Tavily returns no title of its own)."""
    m = _MD_HEADING_RE.search((text or "")[:WORDING_SCAN_CHARS])
    return m.group(1).strip() if m else ""


def _title_parts(title: str) -> list[str]:
    title = " ".join((title or "").split())
    if not title:
        return []
    parts = [title] + [p.strip() for p in TITLE_SEPARATOR_RE.split(title)]
    return list(dict.fromkeys(p for p in parts if p))


def _url_parts(url: str):
    try:
        return urlsplit(url or "")
    except ValueError:
        return None


def _path_segments(parts) -> list[str]:
    return [segment.lower().rsplit(".", 1)[0] for segment in parts.path.split("/") if segment] if parts else []


def _wall_url(final_url: str, url: str = "") -> bool:
    """final_url names a login wall or challenge that the requested url did not already name.

    On the requested host only the last path segment counts (github.com/login, linkedin.com/authwall,
    zhihu.com/account/unhuman), so documentation such as /docs/authenticate/login/auth0-universal-login
    is not a wall. A redirect to another host is a wall when any new segment names one: Google's
    accounts.google.com/v3/signin/identifier for Drive, Docs and the Cloud console."""
    parts = _url_parts(final_url)
    if parts is None:
        return False
    before = _url_parts(url)
    segments = _path_segments(parts)
    known = set(_path_segments(before))
    last = segments[-1] if segments else ""
    host = (parts.hostname or "").lower().removeprefix("www.")
    other_host = host != ((before.hostname or "").lower().removeprefix("www.") if before else "")
    new_walls = {segment for segment in segments if segment in _WALL_SEGMENTS} - known
    return bool(
        (last in new_walls or (other_host and new_walls))
        or (host.split(".")[0] in _WALL_HOST_LABELS and other_host)
        or (_WALL_QUERY_RE.search(parts.query) and not (before and _WALL_QUERY_RE.search(before.query)))
        or "/cdn-cgi/challenge-platform" in parts.path
    )


def _login_title(parts: list[str]) -> bool:
    """A title part (or its text after a "Product:" prefix) is a sign-in title."""
    for part in parts:
        if LOGIN_TITLE_RE.fullmatch(part):
            return True
        tail = _LOGIN_TITLE_PREFIX_RE.split(part, 1)
        if len(tail) == 2 and tail[1] and LOGIN_TITLE_RE.fullmatch(tail[1]):
            return True
    return False


def is_interstitial(title: str = "", text: str = "", *, redirected: bool = False, final_url: str = "",
                    url: str = "", complete: bool = True) -> bool:
    """True when title/text look like a login wall or bot check rather than the requested page.

    text may be raw HTML (the verification layer) or extracted Markdown (web_fetch backends).
    redirected: the request for url ended on another URL (final_url), which makes a wall title, a
    sign-in title on a short page (or one with a password field) or a new sign-in URL sufficient.
    complete: False when text is only the first part of the body (the caller stopped reading); such a
    body never counts as a short page.
    """
    text = text or ""
    head = text[:BODY_SCAN_CHARS]
    visible = visible_text(head)
    whole = complete and len(text) <= BODY_SCAN_CHARS
    short = whole and len(visible) <= WALL_MAX_CHARS
    markup = bool(CHALLENGE_MARKUP_RE.search(head))
    wording = bool(CHALLENGE_WORDING_RE.search(visible[:WORDING_SCAN_CHARS]))
    parts = _title_parts(title)
    hits = [p for p in parts if WALL_TITLE_RE.fullmatch(p)]
    if hits:
        if redirected or markup:
            return True
        if all(_GENERIC_TITLE_RE.fullmatch(p) for p in hits):
            # "CAPTCHA - Wikipedia": an article unless the page is a stub.
            return whole and len(visible) <= GENERIC_MAX_CHARS
        if short or wording:
            return True
    if (markup or wording) and short:
        return True
    if not redirected:
        return False
    # "Sign in with Apple | Apple Developer Documentation" is a long article; a sign-in page is short
    # or carries a password field. Extracted Markdown has its own heading ("# Sign in").
    if short or _PASSWORD_INPUT_RE.search(head):
        if _login_title(parts) or _login_title(_title_parts(markdown_title(text))):
            return True
    return _wall_url(final_url, url)
