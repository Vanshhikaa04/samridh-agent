"""Backend for the browser extension (extension/): given the fields of whatever form you have open, answer the ones the agent can
answer truthfully (same rules, profile and resume-grounded RAG as the bot) and say why it left the rest for you.

Only the extension may call it: every request needs the secret token in data/extension_token.txt (written into extension/config.js).
"""
import re
import secrets
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

from . import rag
from .config import DATA, ROOT, profile

TOKEN_FILE = DATA / "extension_token.txt"
EXT_DIR = ROOT / "extension"
BASE_URL = "http://127.0.0.1:8765"

MARKETING = re.compile(r"newsletter|marketing|promotional|receive (our |.{0,20})?(updates|offers|emails|news)|subscribe|future (job )?opportunit|"
                       r"contact me (about|regarding) (other|future)|sms|text message|talent (network|community)", re.I)
CONSENT = re.compile(r"privacy|terms|consent|gdpr|agree|accept|certify|acknowledg|i declare|attest|i understand|i have read|authori[sz]e|integritet|"
                     r"samtycke|godkänn|personuppgift|behandl|datenschutz|einwillig|akzeptier|zustimm|personal (data|information)|"
                     r"data (will be |is |are )?(processed|stored|held)|processing of|data protection|persoonsgegevens|verwerk|akkoord|"
                     r"dati personali|donn[ée]es personnelles|datos personales|j'accepte|acepto|accetto", re.I)
RELOCATE_GROUP = re.compile(r"where (would|do) you|which office|office|location|city|standort|vestiging|kontor", re.I)


def ensure_token() -> str:
    """Create the secret once and write extension/config.js so the extension carries it."""
    if TOKEN_FILE.exists() and TOKEN_FILE.read_text().strip():
        token = TOKEN_FILE.read_text().strip()
    else:
        token = secrets.token_urlsafe(32)
        TOKEN_FILE.write_text(token)
    EXT_DIR.mkdir(exist_ok=True)
    cfg = f'const JA_BASE = "{BASE_URL}";\nconst JA_TOKEN = "{token}";\n'
    cfg_path = EXT_DIR / "config.js"
    if not cfg_path.exists() or cfg_path.read_text() != cfg:
        cfg_path.write_text(cfg)
    return token


def authorized(headers) -> bool:
    got = headers.get("X-Agent-Token", "")
    try:
        want = TOKEN_FILE.read_text().strip()
    except Exception:
        return False
    return bool(want) and secrets.compare_digest(got, want)


def _job(d: dict) -> dict:
    url = d.get("url", "")
    host = urlparse(url).netloc.lower().removeprefix("www.")
    title = (d.get("title") or "this role").strip()[:120]
    company = re.split(r"[.:]", host)[0] if host else "the company"
    # job-board hosts: the company is usually the first path segment ("jobs.ashbyhq.com/acme/...")
    if re.search(r"ashbyhq|greenhouse|lever\.co|workable|recruitee|smartrecruiters|teamtailor|personio", host):
        seg = [s for s in urlparse(url).path.split("/") if s]
        if seg:
            company = seg[0]
    if re.search(r"intern(ship)?\b|werkstudent|praktik", urlparse(url).path, re.I) and not re.search(r"intern|werkstudent|praktik", title, re.I):
        title += " (Internship)"  # the URL says so even if the page title does not: use the Student resume
    return {"id": "ext:" + url[:80], "title": title, "company": company.replace("-", " ").title(), "description": (d.get("page_text") or "")[:8000], "url": url}


def _answer_field(f: dict, job: dict) -> dict:
    from . import apply as A  # reuse the bot's own rules
    kind, label = f.get("kind", "text"), (f.get("label") or "").strip()
    typ = (f.get("type") or "").lower()
    group = f.get("group", "")
    ctx = label + " " + group

    if kind == "file":
        if re.search(r"cover|motivation|letter|brev|anschreiben|lettre", ctx, re.I):
            return {"action": "upload", "which": "cover"} if A._cover_pdf() else {"action": "skip", "reason": "no cover letter file set up"}
        if re.search(r"resume|cv|curriculum|lebenslauf|meritförteckning", ctx, re.I) or not label:
            return {"action": "upload", "which": "resume"}
        return {"action": "skip", "reason": "unknown upload - left for you"}

    if kind == "checkbox":
        if MARKETING.search(ctx):
            return {"action": "skip", "reason": "optional marketing box left unticked"}
        if CONSENT.search(ctx):
            return {"action": "check", "via": "consent"}
        if RELOCATE_GROUP.search(group):
            return {"action": "check", "via": "relocation-ok"}
        return {"action": "skip", "reason": "checkbox - your call" if f.get("required") else "optional checkbox left as is"}

    if kind in ("radio", "select"):
        opts = [{"id": str(o["id"]), "label": o["label"]} for o in f.get("options", [])]
        if not opts:
            return {"action": "skip", "reason": "no options found"}
        o, ans, via = A._pick({"label": label, "options": opts, "kind": kind, "required": f.get("required", False)}, job)
        if o is None:
            return {"action": "skip", "reason": f"needs you ({via})"}
        return {"action": "select", "option_id": o["id"], "value": o["label"], "via": via}

    # text / textarea / email / tel / number / date
    if re.search(r"newsletter|subscribe|subscription", ctx, re.I):
        return {"action": "skip", "reason": "newsletter box"}
    val, via = A._personal(label, typ), "profile"
    if typ == "date" or re.search(r"start date|available (from|to start)|earliest (start|available)|when can you start", label, re.I):
        val = (date.today() + timedelta(days=14)).isoformat() if typ == "date" else profile()["notice_period"]
    if val is None and kind == "textarea" and re.search(r"cover|letter|motivation|brev|anschreiben|lettre", label, re.I):
        val, via = A._cover_text(job), "cover_pdf"
    if val is None and re.search(r"years? of (work )?experience|how many years", label, re.I) and not re.search(r"\bwith\b|\bin\b|using", label, re.I):
        val = str(profile()["years_experience"])
    if val is None and (f.get("required") or kind == "textarea") and label:
        try:
            val, via = rag.answer(label, job)
        except Exception as e:
            return {"action": "skip", "reason": f"answer engine error: {type(e).__name__}"}
        if via == "NEEDS_USER":
            return {"action": "skip", "reason": "needs you - not in your resume/profile"}
    if not val:
        return {"action": "skip", "reason": "optional - left blank" if not f.get("required") else "needs you"}
    if typ == "number":
        m = re.search(r"\d+(?:\.\d+)?", str(val))
        val = m.group(0) if m else val
    return {"action": "fill", "value": str(val)[:2000], "via": via}


def _tailored_resume(job: dict) -> dict:
    """Tailor the resume's skills rows to this page's job description (nothing is added that your resume does not back up)."""
    from . import tailor
    try:
        t = tailor.tailor(job)
        rep = t["report"]
        added = [x for r in rep["rows"].values() for x in r["added"]]
        reworded = [z["to"] for r in rep["rows"].values() for z in r["reworded"]]
        return {"ok": True, "added": added, "reworded": reworded, "not_added": rep.get("not_added", [])[:8],
                "base": Path(t["path"]).name}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:80]}"}


def fill(d: dict) -> dict:
    job = _job(d)
    out = []
    resume_note = None
    for f in d.get("fields", [])[:120]:
        try:
            r = _answer_field(f, job)
        except Exception as e:  # one odd field never blocks the rest of the form
            r = {"action": "skip", "reason": f"error: {type(e).__name__}"}
        if r.get("action") == "upload" and r.get("which") == "resume":
            if resume_note is None:
                resume_note = _tailored_resume(job)  # once per page, cached by job id
            r["jid"] = job["id"]
        r["id"] = f["id"]
        r["label"] = (f.get("label") or "")[:100]
        out.append(r)
    return {"answers": out, "company": job["company"], "title": job["title"], "resume": resume_note}


def file_bytes(which: str, jid: str = "") -> tuple[bytes, str] | None:
    from . import apply as A, db, tailor
    p: Path | None = A._cover_pdf() if which == "cover" else tailor.base_resume()
    if which == "resume" and jid:  # the copy tailored for this page's job
        row = db.conn().execute("SELECT path FROM resumes WHERE job_id=?", (jid,)).fetchone()
        if row and Path(row["path"]).exists():
            p = Path(row["path"])
    return (p.read_bytes(), p.name) if p and p.exists() else None
