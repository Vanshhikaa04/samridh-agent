"""Apply by email, for jobs whose ad says 'send your application to <address>' and has no web form.

If SMTP is configured in .env the email is sent; otherwise a ready-to-send .eml draft is saved to data/email_drafts/
(double-click it, it opens in your mail app with the CV and cover letter attached - press Send).
"""
import re
import smtplib
import ssl
from email.message import EmailMessage

import httpx

from .config import DATA, env, profile

DRAFTS = DATA / "email_drafts"
DRAFTS.mkdir(exist_ok=True)


def application_target(job: dict) -> tuple[str | None, str | None]:
    """(email, url) from the official Platsbanken/JobTech record, for Swedish ads whose stored URL is the ad page."""
    m = re.search(r"platsbanken/annonser/(\d+)", job["url"])
    if not m:
        return None, None
    try:
        d = httpx.get(f"https://jobsearch.api.jobtechdev.se/ad/{m.group(1)}", timeout=20).json().get("application_details") or {}
        return d.get("email"), d.get("url")
    except Exception:
        return None, None


def _attachments(job: dict | None = None) -> list:
    from . import tailor
    base = tailor.base_resume(job) if job else None
    out = [base] if base else [p for p in sorted(DATA.glob("*[Rr]esume*.pdf"))[:1]]
    out += [p for p in sorted(DATA.glob("*[Cc]over*.pdf"))[:1]]
    return out


def build(job: dict, to: str) -> EmailMessage:
    from .apply import _cover_text
    p = profile()
    msg = EmailMessage()
    msg["To"] = to
    msg["From"] = env("SMTP_FROM") or p["email"]
    msg["Subject"] = f"Application: {job['title']} - {p['name']}"
    body = (_cover_text(job) or f"Hello,\n\nI would like to apply for the {job['title']} position.\n\nBest regards,\n{p['name']}")
    msg.set_content(body + f"\n\n--\n{p['name']}\n{p['email']} | {p['phone']}\n{p['linkedin']}\n{p['github']}")
    for f in _attachments(job):
        msg.add_attachment(f.read_bytes(), maintype="application", subtype="pdf", filename=f.name)
    return msg


def apply_by_email(job: dict, to: str, send: bool) -> tuple[str, str]:
    """Returns (status, detail): 'submitted' (really sent via SMTP) | 'drafted' (saved .eml) | 'failed'."""
    msg = build(job, to)
    if send and env("SMTP_HOST") and env("SMTP_USER") and env("SMTP_PASSWORD"):
        try:
            port = int(env("SMTP_PORT", "465"))
            if port == 465:
                with smtplib.SMTP_SSL(env("SMTP_HOST"), port, context=ssl.create_default_context(), timeout=30) as s:
                    s.login(env("SMTP_USER"), env("SMTP_PASSWORD")); s.send_message(msg)
            else:
                with smtplib.SMTP(env("SMTP_HOST"), port, timeout=30) as s:
                    s.starttls(context=ssl.create_default_context()); s.login(env("SMTP_USER"), env("SMTP_PASSWORD")); s.send_message(msg)
            return "submitted", f"emailed to {to} with CV + cover letter (SMTP accepted the message)"
        except Exception as e:
            return "failed", f"SMTP error: {type(e).__name__}: {str(e)[:120]}"
    path = DRAFTS / (re.sub(r"\W+", "_", job["company"])[:30] + ".eml")
    path.write_bytes(bytes(msg))
    return "drafted", f"email to {to} ready - open {path} and press Send (set SMTP_* in .env to send automatically)"
