"""One-time logins for sites that only let signed-in users apply (Wellfound, Internshala, Unstop).

Open  http://localhost:8765/logins  (or:  python -m jobagent site-login wellfound).

A NORMAL Google Chrome window opens (no automation attached, so "Sign in with Google" works: Google refuses to sign anyone in inside a
script-controlled browser). You sign in yourself. When you press Save session, the agent connects to that Chrome only to copy the site's
session cookies into data/auth/<site>.json, which the form-filler reuses for that site only. No password is ever read or stored.
"""
import os
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from .config import DATA

AUTH = DATA / "auth"
PROFILE = AUTH / "chrome_profile"  # a separate Chrome profile just for these logins (your own Chrome profile is never touched)
PORT = 9333
SITES = {
    "internshala": ("https://internshala.com/login/student", "internshala.com"),
    "unstop": ("https://unstop.com/auth/login", "unstop.com"),
    "wellfound": ("https://wellfound.com/login", "wellfound.com"),
    "yc": ("https://account.ycombinator.com/?continue=https%3A%2F%2Fwww.workatastartup.com%2F", "ycombinator.com"),
}
CHROME_PATHS = [r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")]


def _site_for(url: str) -> str | None:
    host = urlparse(url).netloc.lower()
    return next((s for s, (_, dom) in SITES.items() if host == dom or host.endswith("." + dom)), None)


def state_for(url: str):
    s = _site_for(url)
    p = AUTH / f"{s}.json" if s else None
    return p if p and p.exists() else None


def needs_login(url: str) -> str | None:
    """The site name if this URL is on a login-only site and you have not logged in yet."""
    s = _site_for(url)
    return s if s and not (AUTH / f"{s}.json").exists() else None


def _chrome() -> str | None:
    return next((p for p in CHROME_PATHS if Path(p).exists()), None)


def _open_chrome(url: str):
    exe = _chrome()
    if not exe:
        raise RuntimeError("Google Chrome was not found on this computer")
    PROFILE.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen([exe, f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}", "--no-first-run", "--no-default-browser-check",
                             "--new-window", url])


# (url, text that appears ONLY when you are NOT signed in)
NOT_LOGGED_IN = {
    "yc": ("https://account.ycombinator.com/", "Log in or sign up"),
    "wellfound": ("https://wellfound.com/jobs", "Log in"),
}


def _logged_in(site: str) -> bool:
    """True if the login Chrome window is really signed in to this site (checked in a new tab of that same window)."""
    if site not in NOT_LOGGED_IN:
        return True
    url, marker = NOT_LOGGED_IN[site]
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
        page = browser.contexts[0].new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2500)
            return marker not in page.inner_text("body")[:600]
        finally:
            page.close()


def _save_state(site: str):
    """Copy this Chrome's session cookies (for every site you logged into there) into the site's state file."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
        browser.contexts[0].storage_state(path=str(AUTH / f"{site}.json"))


# ------------------------------------------------------------------ dashboard (/logins) and CLI
_SESSIONS: dict = {}


def start_login(site: str) -> dict:
    if site not in SITES:
        return {"ok": False, "msg": "unknown site"}
    cur = _SESSIONS.get(site)
    if cur and cur["state"] == "open":
        return {"ok": True, "msg": "The Chrome window is already open."}
    rec = {"event": threading.Event(), "state": "open", "proc": None,
           "msg": "A Chrome window opened on this computer: sign in there (Google sign-in works), then press Save session here."}
    _SESSIONS[site] = rec
    try:
        rec["proc"] = _open_chrome(SITES[site][0])
    except Exception as e:
        rec.update(state="error", msg=f"Could not open Chrome: {e}")
        return {"ok": False, "msg": rec["msg"]}

    def work():
        try:
            deadline = time.time() + 1800
            while time.time() < deadline:
                if not rec["event"].wait(timeout=max(1, deadline - time.time())):
                    break
                rec["event"].clear()
                if not _logged_in(site):  # pressed Save before the sign-in finished: do not save a useless session
                    rec.update(state="open", msg=f"You are NOT signed in to {site} yet in that Chrome window. Finish signing in (you should see your account), then press Save session again.")
                    continue
                _save_state(site)
                rec.update(state="saved", msg=f"Saved and verified: you are signed in. The agent can now apply on {site}. You can close the Chrome window.")
                _requeue(site)
                return
            rec.update(state="error", msg="Timed out after 30 minutes. Open the window again.")
        except Exception as e:
            rec.update(state="error", msg=f"Could not save ({type(e).__name__}). Keep the Chrome window open, sign in, then press Save session.")

    threading.Thread(target=work, daemon=True, name=f"login-{site}").start()
    return {"ok": True, "msg": rec["msg"]}


def save_login(site: str) -> dict:
    rec = _SESSIONS.get(site)
    if not rec or rec["state"] != "open":
        return {"ok": False, "msg": "No login window is open for this site."}
    rec["event"].set()
    return {"ok": True, "msg": "Saving..."}


def _requeue(site: str):
    """Jobs that were waiting for this login go back into the queue."""
    from . import db
    c = db.conn()
    c.execute("UPDATE jobs SET status='ready', notes='login saved: retry' WHERE status='needs_input' AND notes LIKE ?", (f"log in once%{site}%",))
    c.commit()


def status() -> dict:
    from . import db
    c = db.conn()
    out = {}
    for site in SITES:
        rec = _SESSIONS.get(site, {})
        waiting = c.execute("SELECT COUNT(*) FROM jobs WHERE status='needs_input' AND notes LIKE ?", (f"log in once%{site}%",)).fetchone()[0]
        out[site] = {"logged_in": (AUTH / f"{site}.json").exists(), "state": rec.get("state", ""), "msg": rec.get("msg", ""), "waiting": waiting}
    return out


def login(site: str):
    """Terminal version: python -m jobagent site-login wellfound"""
    if site not in SITES:
        print("Sites:", ", ".join(SITES))
        return
    r = start_login(site)
    print(r["msg"])
    if not r["ok"]:
        return
    input("When you are signed in, press Enter here to save the session... ")
    save_login(site)
    for _ in range(40):
        time.sleep(0.5)
        if _SESSIONS[site]["state"] != "open":
            print(_SESSIONS[site]["msg"])
            return
