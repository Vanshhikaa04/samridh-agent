"""Free email verification (no credits): ask the company's own mail server whether a mailbox exists, without sending anything.

MX lookup over DNS-over-HTTPS, then an SMTP conversation up to RCPT TO (the server says 250 = exists, 550 = no such user) and QUIT before any
message is sent. Same technique as the open-source Reacher / check-if-email-exists. Needs outbound port 25 (open on this PC).

Honest limits: a "catch-all" domain accepts every address (we detect that and return 'unknown'), some providers hide the answer or throttle
(also 'unknown'). So: 'valid' and 'invalid' are trustworthy, 'unknown' means "can't tell".
"""
import random
import smtplib
import socket
import string
import time

import httpx

from .config import env

_MX_CACHE: dict[str, list[str]] = {}
_CATCHALL: dict[str, bool] = {}


def mx_hosts(domain: str) -> list[str]:
    if domain in _MX_CACHE:
        return _MX_CACHE[domain]
    hosts: list[str] = []
    try:
        r = httpx.get("https://dns.google/resolve", params={"name": domain, "type": "MX"}, timeout=15).json()
        rows = sorted(((int(a["data"].split()[0]), a["data"].split()[1].rstrip(".")) for a in r.get("Answer", []) if a.get("type") == 15))
        hosts = [h for _, h in rows]
    except Exception:
        pass
    _MX_CACHE[domain] = hosts
    return hosts


def _probe(host: str, address: str, helo: str, mail_from: str, timeout: int = 12) -> int | None:
    """SMTP code the server gives for RCPT TO:<address>, or None if the conversation failed."""
    try:
        with smtplib.SMTP(timeout=timeout) as s:
            s.connect(host, 25)
            s.ehlo(helo)
            code, _ = s.mail(mail_from)
            if code >= 400:
                return None
            code, _ = s.rcpt(address)
            try:
                s.rset()
            except Exception:
                pass
            return code
    except (smtplib.SMTPException, socket.timeout, OSError):
        return None


def verify(address: str) -> str:
    """'valid' | 'invalid' | 'unknown'"""
    domain = address.split("@")[-1].lower()
    hosts = mx_hosts(domain)
    if not hosts:
        return "invalid" if not _resolves(domain) else "unknown"
    helo = env("VERIFY_HELO", "verify.local")
    mail_from = env("SMTP_USER", "me@example.com")
    host = hosts[0]
    if domain not in _CATCHALL:  # a made-up address that is "accepted" means the domain accepts everything
        fake = "".join(random.choices(string.ascii_lowercase, k=14)) + "@" + domain
        c = _probe(host, fake, helo, mail_from)
        _CATCHALL[domain] = c is not None and 200 <= c < 300
        time.sleep(0.5)
    if _CATCHALL[domain]:
        return "unknown"
    code = _probe(host, address, helo, mail_from)
    time.sleep(0.5)
    if code is None:
        return "unknown"
    if 200 <= code < 300:
        return "valid"
    if code in (550, 551, 553, 554):
        return "invalid"
    return "unknown"  # 4xx (greylisting / rate limit) etc.


def _resolves(domain: str) -> bool:
    try:
        socket.gethostbyname(domain)
        return True
    except OSError:
        return False


def candidates(first: str, last: str, domain: str) -> list[str]:
    """The usual startup address formats, most common first."""
    import re
    f, l = re.sub(r"[^a-z]", "", first.lower()), re.sub(r"[^a-z]", "", last.lower())
    pats = [f] if not l else [f, f"{f}.{l}", f"{f}{l}", f"{f[0]}{l}", f"{f}{l[0]}", f"{f[0]}.{l}", f"{f}_{l}", l]
    seen, out = set(), []
    for p in pats:
        if p and p not in seen:
            seen.add(p)
            out.append(f"{p}@{domain}")
    return out


def find(first: str, last: str, domain: str, max_probes: int = 6) -> tuple[str | None, str]:
    """-> (address, how): the first candidate the mail server confirms. ('guess', ...) only when the domain can't be probed."""
    cands = candidates(first, last, domain)[:max_probes]
    unknown = 0
    for a in cands:
        v = verify(a)
        if v == "valid":
            return a, "verified"
        if v == "unknown":
            unknown += 1
    if unknown == len(cands):  # catch-all / hidden answers: nothing to verify against
        return (cands[0] if cands else None), "unverifiable"
    return None, "none-exist"  # every candidate was refused: do not send
