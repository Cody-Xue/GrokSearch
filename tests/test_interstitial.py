"""The shared login-wall / bot-check detector (interstitial.py), used by web_fetch and URL verification."""
import time

import httpx
import pytest

from grok_search import fetching, interstitial, verify
from grok_search.interstitial import is_interstitial, markdown_title, visible_text

PUBLIC_IP = "93.184.216.34"

# Shapes recorded live (v1.12.1 field report): Reddit serves every script the same 8 KB page whose
# challenge form sits behind an inline SVG; the "Prove your humanity" wording only appears after JS.
REDDIT_JS_CHALLENGE = (
    "<!DOCTYPE html><html><head><title>Reddit</title><meta charset='utf-8'></head><body>"
    "<svg xmlns='http://www.w3.org/2000/svg'>" + "<path d='M0 0L10 10'/>" * 400 + "</svg>"
    "<form method='post'><input type='hidden' name=\"js_challenge\" value=\"1\">"
    "<input type='hidden' name=\"jsc_token\" value=\"abc\"></form>"
    "<script>document.forms[0].submit()</script></body></html>"
)
# Tavily's rendering of the same page.
REDDIT_TAVILY = (
    "[![Image 1](blob:http://localhost/77f24a5f6b40768701decac547643e78)](https://www.reddit.com/)\n\n"
    "# Prove your humanity\n\nWe’re committed to safety and security. But not for bots. Complete the challenge "
    "below and let us know you’re a real person.\n\n[Reddit, Inc. © \"2026\". All rights reserved.](https://www.redditinc.com/)"
)
PYPI_CLIENT_CHALLENGE = (
    "<!DOCTYPE html><html><head><title>Client Challenge</title>"
    "<script src='/_fs-ch-1T1wmsGaOgGaSxcX/script.js'></script></head>"
    "<body><noscript>JavaScript is disabled in your browser.</noscript><p>A required part of this site couldn’t load.</p></body></html>"
)
BAIDU_CHECK = "<html><head><title>百度安全验证</title></head><body><p>网络不给力，请稍后重试</p><a>返回首页</a></body></html>"
WIKI_CAPTCHA = (
    "<!DOCTYPE html><html><head><title>CAPTCHA - Wikipedia</title>"
    "<script src='/cdn-cgi/challenge-platform/scripts/jsd/main.js'></script></head><body><h1>CAPTCHA</h1><p>"
    + "A CAPTCHA is a type of challenge–response test used in computing to determine whether the user is human. " * 20
    + "</p></body></html>"
)


@pytest.mark.parametrize("title, text", [
    ("Reddit - Prove your humanity", "x" * 300),
    ("Prove your humanity", ""),
    ("Reddit", REDDIT_JS_CHALLENGE),
    ("", REDDIT_TAVILY),
    (markdown_title(REDDIT_TAVILY), REDDIT_TAVILY),
    ("Client Challenge", PYPI_CLIENT_CHALLENGE),
    ("", PYPI_CLIENT_CHALLENGE),
    ("百度安全验证", BAIDU_CHECK),
    ("Just a moment...", ""),
    ("Attention Required! | Cloudflare", "short"),
    ("Are you a robot?", "short"),
    ("Verify you are human", "short"),
    ("Example | Verify you are human", "short"),
    ("人机验证 - 示例站", "请完成安全验证"),
    ("安全验证 - 知乎", "请您登录后查看更多专业优质内容。"),
])
def test_bot_checks_are_flagged(title, text):
    assert is_interstitial(title, text) is True


@pytest.mark.parametrize("title, text", [
    ("CAPTCHA - Wikipedia", WIKI_CAPTCHA),  # an article, even with Cloudflare's script on the page
    ("CAPTCHA - Wikipedia", "CAPTCHA article text. " * 2000),
    ("验证码 - 维基百科，自由的百科全书", "验证码是一种区分用户是计算机还是人的公共全自动程序。" * 40),
    ("如何实现短信验证码登录 - 知乎", "正文" * 2000),
    ("Reddit - Dive into anything", "post text " * 400),
    ("Captcha solving with ML: a survey", "short"),
    ("captcha · PyPI", "# captcha\n\nA captcha library that generates audio and image CAPTCHAs. " * 30),
    ("", "Release notes: we now verify you are human only on the login page. " * 40),
])
def test_ordinary_pages_are_not_flagged(title, text):
    assert is_interstitial(title, text) is False


def test_a_generic_title_needs_a_stub_page():
    assert is_interstitial("CAPTCHA - Wikipedia", "x" * 300) is True
    assert is_interstitial("CAPTCHA - Wikipedia", "x" * 1200) is False


def test_spa_page_with_cloudflare_bot_management_script_is_not_a_challenge():
    page = ("<html><head><title>Dashboard</title><script src='/cdn-cgi/challenge-platform/scripts/jsd/main.js'></script>"
            "</head><body><div id='root'></div></body></html>")
    assert is_interstitial("Dashboard", page) is False


def test_redirects_to_sign_in_pages():
    assert is_interstitial("Sign in to GitHub · GitHub", "", redirected=True, final_url="https://github.com/login?return_to=%2Fx") is True
    assert is_interstitial("LinkedIn", "x" * 5000, redirected=True, final_url="https://www.linkedin.com/authwall?trk=x") is True
    assert is_interstitial("New slug", "x" * 5000, redirected=True, final_url="https://example.test/blog/new-slug") is False
    assert is_interstitial("Sign in to GitHub · GitHub", "x" * 5000) is False  # no redirect: the login page itself


def test_visible_text_ignores_scripts_styles_and_svg():
    page = "<html><head><style>p{}</style><script>var a='<b>';</script></head><body><svg><text>X</text></svg><p>Hi &amp; bye</p></body></html>"
    assert visible_text(page) == "Hi & bye"
    assert visible_text("# Markdown stays as it is") == "# Markdown stays as it is"


def test_visible_text_is_linear_on_hostile_markup():
    hostile = "<html>" + "<script>" * 20000 + "x" * 1000
    started = time.monotonic()
    assert visible_text(hostile) == ""
    assert visible_text("<html>" + "<script></script>" * 10000) == ""
    assert time.monotonic() - started < 1.0


def test_markdown_title_and_url_key():
    assert markdown_title("[![img](x)](y)\n\n# Prove your humanity\n\ntext") == "Prove your humanity"
    assert markdown_title("no heading") == ""
    assert interstitial.url_key("http://www.Example.test/a/") == interstitial.url_key("https://example.test/a")


# ------------------------------------------------------------ verification hook (check_url / _url_outcome)
@pytest.fixture
def public_dns(monkeypatch):
    async def resolve(host, port):
        return [PUBLIC_IP]

    monkeypatch.setattr(verify, "resolve_host", resolve)


def site(pages):
    async def handler(request):
        status, body = pages[request.url.path]
        return httpx.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8"})
    return handler


@pytest.mark.asyncio
async def test_check_url_marks_a_2xx_bot_check_and_verification_does_not_count_it(monkeypatch, public_dns):
    pages = {
        "/r/Python/": (200, REDDIT_JS_CHALLENGE),
        "/project/captcha/": (200, PYPI_CLIENT_CHALLENGE),
        "/s": (200, BAIDU_CHECK),
        "/wiki/CAPTCHA": (200, WIKI_CAPTCHA),
    }
    real = httpx.AsyncClient
    monkeypatch.setattr(verify.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(site(pages)), **kwargs))
    base = "https://site.example.test"
    result = await verify.verify_answer("x", [{"url": base + path} for path in pages], check_urls=True, timeout_s=5)
    assert [u["url"] for u in result["urls"]] == [f"{base}/wiki/CAPTCHA"]
    failed = {entry["value"]: entry["reason"] for entry in result["lookup_failed"]}
    assert failed == {base + path: "bot check / interstitial (page may exist)" for path in ("/r/Python/", "/project/captcha/", "/s")}
    assert result["summary"]["urls_reachable"] == 1 and result["summary"]["lookup_failed"] == 3


@pytest.mark.asyncio
async def test_check_url_flags_a_redirect_to_a_login_wall(monkeypatch, public_dns):
    async def handler(request):
        if request.url.path == "/in/someone":
            return httpx.Response(302, headers={"location": "https://site.example.test/authwall?trk=x"})
        return httpx.Response(200, text="<title>LinkedIn</title>" + "<p>Join now</p>" * 400)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), event_hooks={"request": [verify._destination_guard()]}) as client:
        info = await verify.check_url("https://site.example.test/in/someone", client)
    assert info["status"] == 200 and info["interstitial"] is True
    assert verify._url_outcome(info) == ("lookup_failed", "bot check / interstitial (page may exist)")


def test_url_outcome_keeps_ordinary_pages_reachable():
    assert verify._url_outcome({"url": "https://a.test/", "status": 200, "title": "A"}) == ("urls", "")


# ------------------------------------------------------------ review round: hostile markup, auth docs, cut bodies
def test_tag_stripping_is_linear_on_unclosed_angle_brackets():
    """<[^>]*> scanned to the end from every "<": 4.8 s for 100 KB, 2 s per cited URL."""
    started = time.monotonic()
    assert visible_text("<html>" + "<" * 100_000) == "<" * 100_000
    assert is_interstitial("Reddit", "<html>" + "<" * 100_000) is False
    assert time.monotonic() - started < 0.5
    assert visible_text("<html><body><p>a <b>b</b> <i class='x'>c</i></p></body></html>") == "a b c"


AUTH_DOC = "<html><head><title>{title}</title></head><body>" + "<p>Configure the hosted login flow for your tenant.</p>" * 80 + "</body></html>"


@pytest.mark.parametrize("url, final_url, title", [
    # Recorded live: documentation about signing in that moved; v1.12.0 listed all of them as reachable.
    ("https://auth0.com/docs/login", "https://auth0.com/docs/authenticate/login", "Login Authentication on Auth0 - Auth0 Docs"),
    ("https://auth0.com/docs/universal-login", "https://auth0.com/docs/authenticate/login/auth0-universal-login",
     "Auth0 Universal Login - Auth0 Docs"),
    ("https://developer.apple.com/sign-in-with-apple", "https://developer.apple.com/sign-in-with-apple/get-started/",
     "Sign in with Apple | Apple Developer Documentation"),
    # The real page: a script shell whose only visible text is its title.
    ("https://developer.apple.com/sign-in-with-apple", "https://developer.apple.com/design/human-interface-guidelines/sign-in-with-apple",
     "Sign in with Apple | Apple Developer Documentation"),
    ("https://clerk.com/docs/components/authentication/sign-in",
     "https://clerk.com/docs/reference/components/authentication/sign-in", "<SignIn /> component | Clerk"),
])
def test_moved_documentation_about_signing_in_is_not_a_login_wall(url, final_url, title):
    page = AUTH_DOC.format(title=title)
    assert is_interstitial(title, page, redirected=True, final_url=final_url, url=url) is False
    shell = f"<html><head><title>{title}</title><script src='/app.js'></script></head><body><div id='app'></div></body></html>"
    assert is_interstitial(title, shell, redirected=True, final_url=final_url, url=url) is False


def test_real_login_walls_are_still_flagged_after_a_redirect():
    form = "<html><title>Sign in · GitHub</title><form><input name='login'><input type=\"password\" name='p'></form>" + "<p>footer</p>" * 400
    assert is_interstitial("Sign in · GitHub", form, redirected=True, final_url="https://example.test/session", url="https://example.test/private") is True
    assert is_interstitial("LinkedIn", "x" * 5000, redirected=True, final_url="https://www.linkedin.com/authwall?trk=x",
                           url="https://www.linkedin.com/posts/someone") is True
    assert is_interstitial("Medium", "x" * 5000, redirected=True, final_url="https://medium.com/m/signin?redirect=x",
                           url="https://medium.com/@a/post") is True
    assert is_interstitial("Sign in - Google Accounts", "Email or phone", redirected=True,
                           final_url="https://accounts.google.com/v3/signin/identifier", url="https://docs.google.com/d/1") is True


# GitHub issue page: 250-360 KB of HTML whose first 64 KB is <head> and navigation (585 visible characters).
GITHUB_ISSUE_HEAD = ("<!DOCTYPE html><html><head><title>Access Denied · Issue #198 · vadimgrn/usbip-win2 · GitHub</title>"
                     + "<meta name='x' content='y'>" * 2400 + "</head><body><nav>Skip to content Sign in</nav>")


def test_a_cut_off_body_is_not_a_short_page():
    title = "Access Denied · Issue #198 · vadimgrn/usbip-win2 · GitHub"
    assert len(GITHUB_ISSUE_HEAD) < 65536 and len(visible_text(GITHUB_ISSUE_HEAD)) < 100
    assert is_interstitial(title, GITHUB_ISSUE_HEAD, complete=False) is False
    assert is_interstitial(title, GITHUB_ISSUE_HEAD) is True  # the same bytes as a whole page would be a stub
    assert is_interstitial("security check · Issue #49 · hubwriter/todo-page", GITHUB_ISSUE_HEAD + "x" * 70000) is False
    # A wall title with challenge markup still counts in a cut-off body; markup alone needs a whole short page.
    big = REDDIT_JS_CHALLENGE + "<p>x</p>" * 10000
    assert is_interstitial("Reddit - Prove your humanity", big, complete=False) is True
    assert is_interstitial("Reddit", big, complete=False) is False
    assert is_interstitial("Reddit", REDDIT_JS_CHALLENGE) is True


@pytest.mark.asyncio
async def test_check_url_does_not_call_a_large_issue_page_a_bot_check(monkeypatch, public_dns):
    page = GITHUB_ISSUE_HEAD + "<p>The driver fails with access denied.</p>" * 6000  # ~300 KB

    async def handler(request):
        return httpx.Response(200, text=page, headers={"content-type": "text/html; charset=utf-8"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), event_hooks={"request": [verify._destination_guard()]}) as client:
        info = await verify.check_url("https://github.com/vadimgrn/usbip-win2/issues/198", client)
    assert info["status"] == 200 and "interstitial" not in info
    assert verify._url_outcome(info) == ("urls", "")


# Recorded live (RR1-R1): web_fetch https://drive.google.com/drive/my-drive through Firecrawl.
GOOGLE_SIGNIN_FINAL = ("https://accounts.google.com/v3/signin/identifier?continue=https://drive.google.com/drive/my-drive"
                       "&followup=https://drive.google.com/drive/my-drive&osid=1&passive=1209600&service=wise"
                       "&flowName=GlifWebSignIn&flowEntry=ServiceLogin")
GOOGLE_SIGNIN_MD = ("Loading\n\n![](https://www.gstatic.com/images/branding/googleg_gradient/svg/googleg_gradient_standard_48px.svg)\n\n"
                    "# Sign in\n\nto continue to Google Drive\n\nEmail or phone\n\nForgot email?\n\nType the text you hear or see\n\n"
                    "Not your computer? Use Guest mode to sign in privately. [Learn more about using Guest mode]"
                    "(https://support.google.com/chrome/answer/6130773?hl=en-US)\n\nNext\n\nCreate account\n\n"
                    "- For my personal use\n- For my child\n- For work or my business\n")


@pytest.mark.parametrize("url, title", [
    ("https://drive.google.com/drive/my-drive", "Google Drive: Sign-in"),
    ("https://console.cloud.google.com/", "Google Cloud Platform"),
    ("https://docs.google.com/document/d/1abc/edit", "Google Docs: Sign-in"),
])
def test_google_sign_in_redirect_is_a_login_wall(url, title):
    """v1.12.0 flagged it; the round-1 narrowing to the last path segment ("identifier") lost it."""
    assert is_interstitial(title, GOOGLE_SIGNIN_MD, redirected=True, final_url=GOOGLE_SIGNIN_FINAL, url=url) is True
    # The verification layer reads raw HTML that is cut at 64 KB (never a short page): the URL alone decides.
    assert is_interstitial(title, "<html>" + "<p>x</p>" * 20000, redirected=True, final_url=GOOGLE_SIGNIN_FINAL,
                           url=url, complete=False) is True
    info = fetching._page_info(url, {"final_url": GOOGLE_SIGNIN_FINAL, "title": title}, GOOGLE_SIGNIN_MD)
    assert info["warning"] == "interstitial" and info["final_url"] == GOOGLE_SIGNIN_FINAL


def test_sign_in_titles_and_headings_of_short_pages_after_a_redirect():
    # No sign-in word in the final URL: the "Product: Sign-in" title or the page's own heading decides.
    assert is_interstitial("Google Drive: Sign-in", GOOGLE_SIGNIN_MD, redirected=True,
                           final_url="https://example.test/session", url="https://example.test/private") is True
    assert is_interstitial("Example", GOOGLE_SIGNIN_MD, redirected=True,
                           final_url="https://example.test/session", url="https://example.test/private") is True
    assert is_interstitial("Log-in required", "Please log in.", redirected=True,
                           final_url="https://example.test/s", url="https://example.test/p") is True
    # Not redirected, or a long page: unchanged.
    assert is_interstitial("Google Drive: Sign-in", GOOGLE_SIGNIN_MD) is False
    assert is_interstitial("Docs: Sign-in", "# Sign in\n\n" + "How the hosted sign-in flow works. " * 200, redirected=True,
                           final_url="https://example.test/docs/auth", url="https://example.test/docs/login-flow") is False


def test_a_wall_segment_inside_the_path_needs_a_redirect_to_another_host():
    page = AUTH_DOC.format(title="Universal Login")
    # Same host: documentation moved under /login/ (auth0), not a wall.
    assert is_interstitial("Universal Login", page, redirected=True,
                           final_url="https://auth0.com/docs/authenticate/login/auth0-universal-login",
                           url="https://auth0.com/docs/universal-login") is False
    # Another host whose path names a sign-in step.
    assert is_interstitial("Example", page, redirected=True, final_url="https://id.example.test/signin/v2/identifier",
                           url="https://app.example.test/dashboard") is True
    # A wall segment the requested URL already had is not new.
    assert is_interstitial("Example", page, redirected=True, final_url="https://www.example.test/login/help",
                           url="https://example.test/login/help/") is False
