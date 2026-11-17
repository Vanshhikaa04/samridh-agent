"""Y Combinator companies: when there is no form the agent can fill (the YC job portal needs a YC login; many posts just link to a careers page),
find the company's founders on its YC page and email them your outreach template with your resume. That email counts as the application.

Contact addresses come from, in order: the company's Hunter-known addresses, the company's address pattern (first.last@ ...), Hunter's per-person
finder. Nothing is guessed beyond a pattern a real address of that company showed. Run: python -m jobagent yc-outreach
"""
import html as _html
import json
import logging
import re

import httpx

from . import db, outreach
from .config import env

log = logging.getLogger("agent")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36", "Accept-Language": "en"}
SOURCES = ("yc", "hn_jobs", "hn")


def _slugs(job: dict) -> list[str]:
    m = re.search(r"ycombinator\.com/companies/([^/]+)/", job.get("url", ""))
    out = [m.group(1)] if m else []
    name = re.split(r"\s*\(YC|\s+[Ii]s [Hh]iring|\s*\|", job["company"])[0]
    name = re.sub(r"\b(inc|llc|ltd|gmbh|corp|co)\b\.?", "", name, flags=re.I).strip().lower()
    base = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
    for s in (base, base.replace("-", "")):
        if s and s not in out:
            out.append(s)
    return out


def yc_company(job: dict) -> dict | None:
    """{'name', 'website', 'domain', 'founders': [{'name', 'title'}]} from the company's YC page, or None."""
    for slug in _slugs(job):
        try:
            r = httpx.get(f"https://www.ycombinator.com/companies/{slug}", headers=UA, timeout=30, follow_redirects=True)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        m = re.search(r'data-page="([^"]+)"', r.text)
        if not m:
            continue
        try:
            comp = json.loads(_html.unescape(m.group(1)))["props"]["company"]
        except Exception:
            continue
        # the YC page must be for THIS company: a guessed slug can land on a different company (Flowstate vs LaunchFlow)
        from_url = bool(re.search(r"ycombinator\.com/companies/", job.get("url", "")))
        a = re.sub(r"[^a-z0-9]", "", re.split(r"\s*\(YC|\s+[Ii]s [Hh]iring|\s*\|", job["company"])[0].lower())
        b = re.sub(r"[^a-z0-9]", "", (comp.get("name") or "").lower())
        if not from_url and not (a and b and (a == b or (len(a) >= 5 and a in b) or (len(b) >= 5 and b in a))):
            continue
        site = comp.get("website") or ""
        domain = re.sub(r"^https?://(www\.)?", "", site).split("/")[0].lower()
        founders = [{"name": f.get("full_name", ""), "title": f.get("title") or "Founder"} for f in comp.get("founders", []) if f.get("full_name")]
        if domain and founders:
            return {"name": comp.get("name") or job["company"], "website": site, "domain": domain, "founders": founders, "slug": slug}
    return None


def _guess_allowed(c) -> bool:
    """Guessed addresses can bounce, which hurts your sender reputation: cap them per day (YC_GUESS_MAX_PER_DAY, default 60)."""
    if env("YC_GUESS_EMAILS", "1") != "1":
        return False
    from . import caps
    n = c.execute("SELECT COUNT(*) FROM outreach WHERE source IN ('yc-founder','accel-founder') AND notes LIKE '%(guess)%' AND sent_at > ?", (caps.since() or "1970-01-01",)).fetchone()[0]
    n_today = c.execute("SELECT COUNT(*) FROM outreach WHERE source IN ('yc-founder','accel-founder') AND notes LIKE '%(guess)%' AND date(sent_at)=date('now')").fetchone()[0]
    return min(n, n_today) < int(env("YC_GUESS_MAX_PER_DAY", "60"))


def _emails_for(c, info: dict) -> list[dict]:
    """Founders with an email address (known or pattern-built), best first."""
    domain, found = info["domain"], []
    row = c.execute("SELECT email_pattern, hunter_searched_at FROM companies WHERE domain=? OR name=?", (domain, info["name"])).fetchone()
    pattern = row["email_pattern"] if row else None
    known: list[dict] = []
    if env("HUNTER_API_KEY") and not (row and row["hunter_searched_at"]):
        known, hp = outreach.hunter_domain(domain)
        pattern = hp or pattern
        c.execute("INSERT OR IGNORE INTO companies(name, display, domain) VALUES(?,?,?)", (info["name"], info["name"], domain))
        c.execute("UPDATE companies SET hunter_searched_at=datetime('now'), email_pattern=COALESCE(?, email_pattern), domain=? WHERE name=?", (pattern, domain, info["name"]))
        c.commit()
    for f in info["founders"][:3]:
        first, last = outreach._split(f["name"])
        email, how = None, ""
        for k in known:  # a real address Hunter knows for this person
            if k["name"].lower() == f["name"].lower():
                email, how = k["email"], "hunter"
        if not email and pattern and last:
            email, how = f"{outreach._apply_pattern(pattern, first, last)}@{domain}", "pattern"
        if not email:
            email, how = outreach.hunter_find(domain, first, last), "hunter-finder"
        if not email:
            # Hunter has no credits left: ask the company's own mail server which address exists (free). Only a server-confirmed
            # address is sent to freely; if the domain cannot be probed, ONE capped guess (first founder); if every format is refused, skip.
            from . import emailverify
            addr, v = emailverify.find(first, last, domain)
            if v == "verified":
                email, how = addr, "verified"
            elif v == "unverifiable" and f is info["founders"][0] and _guess_allowed(c) and addr:
                email, how = addr, "guess"
        if email:
            found.append({"name": f["name"], "first": first, "title": f["title"], "email": email.lower(), "how": how})
    return found


def run(limit: int = 15) -> dict:
    """Email founders of YC companies for jobs the agent could not apply to by form. Returns counts."""
    c = db.conn()
    out = {"companies": 0, "emails_sent": 0, "no_email": 0, "no_yc_page": 0}
    marks = ",".join("'" + s + "'" for s in SOURCES)
    rows = c.execute(f"SELECT * FROM jobs WHERE source IN ({marks}) AND status IN ('ready','needs_input') AND COALESCE(notes,'') NOT LIKE '%founder%' "
                     "ORDER BY (status='ready') DESC, score DESC").fetchall()
    for r in rows:
        if out["companies"] >= limit:
            break
        job = dict(r)
        # the cheap route first: a ready job the form-filler can still do is left to it; this is for ones that need a login / have no form
        needs = job["status"] == "needs_input"
        if not needs:
            continue
        if c.execute("UPDATE jobs SET status='applying' WHERE id=? AND status IN ('ready','needs_input')", (job["id"],)).rowcount == 0:
            continue
        c.commit()
        out["companies"] += 1
        info = yc_company(job)
        if not info:
            out["no_yc_page"] += 1
            c.execute("UPDATE jobs SET status='needs_input', notes=COALESCE(notes,'')||' | founder email: no YC company page found' WHERE id=?", (job["id"],))
            c.commit()
            continue
        sent, already = [], []
        for f in _emails_for(c, info)[:2]:
            if c.execute("SELECT 1 FROM outreach WHERE email=?", (f["email"],)).fetchone():
                already.append(f["name"])
                continue  # already mailed (or drafted) this person
            subject, body = outreach.compose(f["first"], info["name"], outreach.DEFAULT_FOCUS)
            role = re.sub(r"\s*\(open application\)", "", job["title"]).strip()
            if "open application" not in job["title"] and role and not re.search(r"\bai\b|machine learning|\bml\b|\bllm\b", role, re.I):
                subject = subject.replace("AI Engineer opportunity", role + " opportunity")  # e.g. "Product Engineer opportunity at Acme"
                body = body.replace("the AI Engineer opportunity", "the " + role + " opportunity", 1)
            cur = c.execute("INSERT OR IGNORE INTO outreach(company, display_company, domain, job_id, name, first_name, title, email, source, confidence, status, subject, body, notes) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (job["company"], info["name"], info["domain"], job["id"], f["name"], f["first"], f["title"], f["email"], "yc-founder",
                             "high" if f["how"] != "pattern" else "medium", "drafted", subject, body, f"YC founder ({f['how']}) for: {job['title'][:80]}"))
            c.commit()
            if cur.rowcount:
                oid = c.execute("SELECT id FROM outreach WHERE email=?", (f["email"],)).fetchone()["id"]
                ok, why = outreach.send_row(c, oid)
                if ok:
                    sent.append(f"{f['name']} ({f['title']}) <{f['email']}>")
                elif "cap" in why:
                    break
        if sent:
            out["emails_sent"] += len(sent)
            c.execute("UPDATE jobs SET status='applied', applied_at=datetime('now'), notes=? WHERE id=?",
                      ("emailed YC founder(s): " + "; ".join(sent) + f" | YC page: ycombinator.com/companies/{info['slug']}", job["id"]))
        elif already:  # no second email to the same founder: this role is applied for through the YC portal (needs the one-time YC login)
            c.execute("UPDATE jobs SET status='ready', notes=? WHERE id=?", (f"founder already emailed ({', '.join(already)}): applying through the YC portal instead", job["id"]))
        else:
            out["no_email"] += 1
            names = ", ".join(f["name"] for f in info["founders"][:3])
            c.execute("UPDATE jobs SET status='needs_input', notes=COALESCE(notes,'')||?||? WHERE id=?",
                      (" | founders: ", f"{names} (YC page found, no email address could be found)", job["id"]))
        c.commit()
    log.info("yc founder outreach: %s", out)
    return out
