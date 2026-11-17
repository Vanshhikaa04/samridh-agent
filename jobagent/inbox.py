"""Keep track of the mail you RECEIVE about applications: rejections, interview invites, replies to your outreach, auto-acknowledgements.

Reads your Gmail inbox over IMAP (read-only), keeps only job-related messages, classifies them, matches each to the company you applied to
or e-mailed, and updates the tracker (job -> 'rejected' / 'interview', outreach -> 'replied'). Run: python -m jobagent inbox
"""
import email
import email.utils
import imaplib
import re
from datetime import datetime, timedelta
from email.header import decode_header, make_header

from . import db
from .config import env

REJECT = re.compile(
    r"regret to (inform|tell)|unfortunately,? (we|i|after|your|the)|not (be )?(moving|move|going) forward|(will|would|can) not be (moving|proceeding)|"
    r"decided (to|not to) (move|proceed|pursue|continue)|(proceed|continue|move forward) with (other|another) (candidate|applicant)|other (candidates|applicants) (have|were|who)|"
    r"not (been )?selected|position (has been|is) (now )?filled|no longer (considering|available)|we won.t be (moving|proceeding)|"
    r"tyvärr|inte gå vidare|har valt att gå vidare med|leider (müssen|haben|können)|absage|malheureusement|n'avons pas retenu|"
    r"lamentablemente|desafortunadamente|helaas", re.I)
INVITE = re.compile(
    r"invite you to (an? )?(interview|call|chat|meeting)|would like to (schedule|invite you|speak with you|talk with you|meet you|set up)|"
    r"(schedule|book|set up) (a|an|your) (call|interview|chat|time|slot|meeting)|calendly\.com|your availability|are you available|"
    r"we.d like to (speak|talk|meet|chat)|invitation to (an? )?interview|inbjuder|einladung zum (vorstellungs)?gespräch|invitation à un entretien", re.I)
NOT_INVITE = re.compile(r"planning to schedule|if you are among|if we find|if your (experience|profile)|we will contact you|we.ll (contact|reach out)|should we|in case", re.I)
ACK = re.compile(
    r"thank(s| you) for (applying|your application|your interest)|we('ve| have) received your application|application (received|submitted|confirmation)|"
    r"we got it|your application (to|for|has been|status)|tack för din ansökan|vielen dank für ihre bewerbung|merci pour votre candidature|gracias por (aplicar|tu candidatura)", re.I)
JOBY = re.compile(r"application|applying|candidate|position|role|hiring|recruit|interview|vacancy|job|career|resume|cv\b|ansökan|bewerbung|candidature|solicitud|sollicitatie", re.I)
NOISE_SENDER = re.compile(r"no-?reply@(linkedin|indeed|glassdoor)|jobalerts|newsletter|digest|notifications?@(linkedin|facebook|google)|boardy|remotive|wellfound|unstop|convertkit|substack|mailchimp|stayingahead|micro1|@(updates|news|mail|hi)\.", re.I)
BOUNCE = re.compile(r"postmaster|mailer-daemon|undeliverable|delivery status|returned mail|delivery has failed|address not found", re.I)


def _h(v) -> str:
    try:
        return str(make_header(decode_header(v or ""))).strip()
    except Exception:  # unknown charset etc.: never let one odd mail stop the scan
        return "".join(x.decode("utf-8", "replace") if isinstance(x, bytes) else x for x, _ in decode_header(v or "")).strip()


def _dec(p) -> str:
    raw = p.get_payload(decode=True) or b""
    for cs in (p.get_content_charset() or "utf-8", "utf-8"):
        try:
            return raw.decode(cs, "replace")
        except LookupError:
            continue
    return raw.decode("latin-1", "replace")


def _body(msg) -> str:
    parts = [msg] if not msg.is_multipart() else list(msg.walk())
    for p in parts:
        if p.get_content_type() == "text/plain":
            try:
                return _dec(p)
            except Exception:
                pass
    for p in parts:
        if p.get_content_type() == "text/html":
            try:
                html = _dec(p)
                return re.sub(r"<[^>]+>|&nbsp;", " ", re.sub(r"(?s)<(script|style).*?</\1>", " ", html))
            except Exception:
                pass
    return ""


def classify(subject: str, body: str) -> str:
    t = subject + " " + body[:1500]
    if REJECT.search(t):
        return "rejection"
    if INVITE.search(t) and not NOT_INVITE.search(t) and not re.search(r"thank(s| you) for applying|we got it|received your application", subject, re.I):
        return "interview"
    if ACK.search(t):
        return "auto_ack"
    return "reply"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _match(c, from_addr: str, subject: str, body: str):
    """-> (company, job_id, outreach_id). By sender domain/address first, then by a company name in the subject/body (ATS mail)."""
    dom = from_addr.split("@")[-1].lower()
    shared = re.compile(r"(ashbyhq|greenhouse|lever|workable|myworkday|workday|teamtailor|personio|smartrecruiters|recruitee|comeet|bamboohr|icims|jobvite|breezy|linkedin|gmail|outlook|hotmail|yahoo|eightfold|oraclecloud|successfactors|taleo|jobs?lever|hire|careers-page|applytojob|rippling|dover|wellfound|ycombinator)", re.I)
    if shared.search(dom):
        dom = "no-company-domain.invalid"  # fall through to matching by the company named in the subject
    o = c.execute("SELECT id, company, job_id FROM outreach WHERE lower(email)=? OR lower(domain)=? ORDER BY (lower(email)=?) DESC LIMIT 1", (from_addr, dom, from_addr)).fetchone()
    reg = ".".join(dom.split(".")[-2:])
    j = c.execute("SELECT id, company FROM jobs WHERE status IN ('applied','interview','offer','rejected','ghosted') AND (lower(url) LIKE ? OR lower(url) LIKE ?) LIMIT 1",
                  (f"%{dom}%", f"%{reg}%")).fetchone()
    if j:
        return j["company"], j["id"], o["id"] if o else None
    hay = _norm(subject)  # the company must be named in the SUBJECT: a body that merely mentions another company (Perplexity in a Sanovio mail) must not change a job's status
    best = None
    for r in c.execute("SELECT id, company FROM jobs WHERE status IN ('applied','interview','offer','rejected','ghosted')").fetchall():
        n = _norm(r["company"])
        if len(n) >= 4 and n in hay and (best is None or len(n) > len(_norm(best["company"]))):
            best = r
    if best:
        return best["company"], best["id"], o["id"] if o else None
    if o:
        return o["company"], o["job_id"], o["id"]
    return None, None, None


def scan(days: int = 45, limit: int = 400) -> dict:
    if not (env("SMTP_USER") and env("SMTP_PASSWORD")):
        return {"error": "needs SMTP_USER / SMTP_PASSWORD (Gmail app password)"}
    c = db.conn()
    me = env("SMTP_USER").lower()
    since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
    new = {"rejection": 0, "interview": 0, "reply": 0, "auto_ack": 0, "bounce": 0}
    try:
        m = imaplib.IMAP4_SSL("imap.gmail.com", timeout=60)
        m.login(env("SMTP_USER"), env("SMTP_PASSWORD"))
        m.select("INBOX", readonly=True)
        typ, d = m.search(None, f"(SINCE {since})")
        ids = d[0].split()[-limit:]
        for i in reversed(ids):
            typ, data = m.fetch(i, "(BODY.PEEK[]<0.60000>)")
            msg = email.message_from_bytes(data[0][1])
            mid = (msg.get("Message-ID") or "").strip() or f"uid:{i.decode()}"
            if c.execute("SELECT 1 FROM inbox WHERE message_id=?", (mid,)).fetchone():
                continue
            name, addr = email.utils.parseaddr(_h(msg.get("From")))
            addr = addr.lower()
            subject = _h(msg.get("Subject"))
            if not addr or addr == me:
                continue
            bounce = bool(BOUNCE.search(addr + " " + subject))
            if NOISE_SENDER.search(addr) and not bounce:
                continue
            body = _body(msg)
            hdr = msg
            company, job_id, oid = _match(c, addr, subject, body)
            if not company and not re.search(r"application|applying|candidacy|your interest|interview|ansökan|bewerbung|candidature|solicitud", subject, re.I):
                continue  # not about a job: never store your private mail
            kind = "bounce" if bounce else classify(subject, body)
            when = email.utils.parsedate_to_datetime(hdr.get("Date")).strftime("%Y-%m-%d %H:%M") if hdr.get("Date") else ""
            snippet = re.sub(r"\s+", " ", body).strip()[:300]
            c.execute("INSERT OR IGNORE INTO inbox(message_id, from_addr, from_name, subject, received_at, snippet, kind, company, job_id, outreach_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (mid, addr, name, subject[:200], when, snippet, kind, company, job_id, oid))
            new[kind] += 1
            if kind == "bounce":
                bad = re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", body) and [e for e in re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", body) if e.lower() != me]
                for e in (bad or []):
                    c.execute("UPDATE outreach SET status='bounced', notes='delivery failed (bounce received)' WHERE lower(email)=? AND status IN ('sent','replied')", (e.lower(),))
            elif job_id and kind == "rejection":
                c.execute("UPDATE jobs SET status='rejected', notes=COALESCE(notes,'')||' | rejection email '||? WHERE id=? AND status IN ('applied','interview','ghosted')", (when, job_id))
            elif job_id and kind == "interview":
                c.execute("UPDATE jobs SET status='interview', notes=COALESCE(notes,'')||' | interview mail '||? WHERE id=? AND status='applied'", (when, job_id))
            if oid and kind in ("rejection", "interview", "reply") and not bounce:
                c.execute("UPDATE outreach SET status='replied', replied_at=COALESCE(replied_at, datetime('now')) WHERE id=? AND status IN ('sent','replied')", (oid,))
            c.commit()
        m.logout()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:120]}", **new}
    return {**new, **scan_bounces(days)}


def scan_bounces(days: int = 14) -> dict:
    """Delivery-failure notices land in Spam as often as in the inbox: read both and mark the failed outreach addresses as bounced."""
    if not (env("SMTP_USER") and env("SMTP_PASSWORD")):
        return {}
    c = db.conn()
    me = env("SMTP_USER").lower()
    since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
    marked, seen = 0, 0
    try:
        m = imaplib.IMAP4_SSL("imap.gmail.com", timeout=60)
        m.login(env("SMTP_USER"), env("SMTP_PASSWORD"))
        for box in ("[Gmail]/Spam", "INBOX"):
            m.select(f'"{box}"', readonly=True)
            typ, d = m.search(None, f'(SINCE {since} FROM "mailer-daemon")')
            for i in d[0].split():
                typ, data = m.fetch(i, "(BODY.PEEK[]<0.40000>)")
                msg = email.message_from_bytes(data[0][1])
                text = _body(msg) + " " + " ".join(str(p.get_payload()) for p in msg.walk() if p.get_content_type() in ("message/delivery-status", "message/rfc822"))
                seen += 1
                for e in set(x.lower() for x in re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text)):
                    if e == me or "mailer-daemon" in e or e.endswith(("google.com", "googlemail.com")):
                        continue
                    marked += c.execute("UPDATE outreach SET status='bounced', notes='delivery failed (bounce notice found)' WHERE lower(email)=? AND status IN ('sent','replied')", (e,)).rowcount
        c.commit()
        m.logout()
    except Exception as e:
        return {"bounce_error": f"{type(e).__name__}: {str(e)[:100]}"}
    return {"bounce_notices": seen, "outreach_marked_bounced": marked}
