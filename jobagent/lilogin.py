"""Optional: use YOUR logged-in LinkedIn session to read where a job's "Apply" arrow really leads.

OFF by default (USE_LINKEDIN_LOGIN=0). You log in yourself, once, in a browser window this opens; the session is kept
in data/linkedin_profile (like a normal browser profile). No password is ever read or stored by this code.

Heads-up: automating a logged-in LinkedIn account is against LinkedIn's terms and can get the account restricted, and
clicking Apply on LinkedIn shares your profile with the job poster. So: free methods run first, it is rate-limited,
it only touches jobs that already passed scoring, and it stops itself at the first sign of a checkpoint/captcha.
"""
import json
import random
import re
import threading
import time
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, unquote, urlparse

from .config import DATA, env

PROFILE = DATA / "linkedin_profile"
STATE = DATA / "linkedin_login_state.json"


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def _save(**kw):
    s = _state()
    s.update(kw)
    STATE.write_text(json.dumps(s))


def enabled() -> tuple[bool, str]:
    if env("USE_LINKEDIN_LOGIN", "0") != "1":
        return False, "disabled (USE_LINKEDIN_LOGIN=0)"
    if not PROFILE.exists():
        return False, "not logged in yet - run: python -m jobagent linkedin-login"
    s = _state()
    if s.get("paused_until") and datetime.fromisoformat(s["paused_until"]) > datetime.now():
        return False, f"paused until {s['paused_until'][:16]} after a LinkedIn warning"
    cap = int(env("LINKEDIN_MAX_PER_DAY", "15"))
    if s.get("day") == str(date.today()) and s.get("count", 0) >= cap:
        return False, f"daily cap of {cap} LinkedIn lookups reached"
    return True, "ok"


def login():
    """Open a visible browser so YOU can sign in. Closes itself once you reach your feed."""
    from playwright.sync_api import sync_playwright
    PROFILE.mkdir(exist_ok=True)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(PROFILE), channel=env("BROWSER_CHANNEL", "msedge"), headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.linkedin.com/login")
        print("Sign in to LinkedIn in the window that opened (complete any verification). It closes by itself when you reach your feed.")
        try:
            page.wait_for_url(re.compile(r"linkedin\.com/(feed|jobs|in/)"), timeout=300000)
            print("Logged in. Session saved.")
        except Exception:
            print("Timed out waiting for login (5 min). Run the command again.")
        ctx.close()


def _external_from_href(href: str) -> str | None:
    qs = parse_qs(urlparse(href).query)
    for k in ("url", "targetUrl"):
        if k in qs and not urlparse(unquote(qs[k][0])).netloc.endswith("linkedin.com"):
            return unquote(qs[k][0])
    return None


BROWSER_LOCK = threading.RLock()  # the LinkedIn profile can only be open in one browser at a time


def external_apply_url(li_url: str) -> tuple[str | None, str]:
    with BROWSER_LOCK:
        return _external_apply_url(li_url)


def _external_apply_url(li_url: str) -> tuple[str | None, str]:
    """Click the job's Apply arrow in your logged-in session and return the company's application URL."""
    ok, why = enabled()
    if not ok:
        return None, why
    m = re.search(r"(\d{6,})(?:\?|$)", li_url.split("?")[0] + "?")
    if not m:
        return None, "no LinkedIn job id in URL"
    from playwright.sync_api import sync_playwright
    s = _state()
    count = s.get("count", 0) if s.get("day") == str(date.today()) else 0
    try:
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(str(PROFILE), channel=env("BROWSER_CHANNEL", "msedge"),
                                                        headless=env("LINKEDIN_HEADLESS", "1") == "1")
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(15000)
            time.sleep(random.uniform(4, 9))  # human-ish pacing
            page.goto(f"https://www.linkedin.com/jobs/view/{m.group(1)}/", wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            if re.search(r"login|checkpoint|authwall|challenge|captcha", page.url):
                _save(paused_until=(datetime.now() + timedelta(hours=24)).isoformat())
                ctx.close()
                return None, "LinkedIn asked for login/verification - paused for 24h (log in again with linkedin-login)"
            if page.get_by_role("button", name=re.compile(r"easy apply", re.I)).count():
                ctx.close()
                return None, "Easy Apply job - can't be applied to from the company's site"
            apply = page.get_by_role("link", name=re.compile(r"^\s*apply", re.I)).or_(
                page.get_by_role("button", name=re.compile(r"^\s*apply", re.I)))
            if not apply.count():
                ctx.close()
                return None, "no Apply button found (job may be closed)"
            btn = apply.first
            href = btn.get_attribute("href") or ""
            ext = _external_from_href(href) if href else None
            if not ext:  # read-the-link failed: do what you do by hand, click it and catch the new tab
                with ctx.expect_page(timeout=20000) as pg:
                    btn.click()
                newp = pg.value
                newp.wait_for_load_state("domcontentloaded", timeout=25000)
                for _ in range(10):
                    if "linkedin.com" not in urlparse(newp.url).netloc:
                        break
                    newp.wait_for_timeout(1000)
                ext = newp.url if "linkedin.com" not in urlparse(newp.url).netloc else None
                newp.close()
            ctx.close()
        _save(day=str(date.today()), count=count + 1)
        return (ext, "ok") if ext else (None, "Apply click did not lead off LinkedIn")
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:120]}"
