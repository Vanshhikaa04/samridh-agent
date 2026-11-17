"""Companies from startup-accelerator portfolios (Seedcamp, Techstars, Antler, Founders Factory, Entrepreneur First, 500 Global): an open application to
the founders of the AI-focused ones.

For each company: read its homepage (is it AI-focused?), find its founders from its own site (team / about pages), find a mailbox for each with
the free verifier (emailverify), send your outreach template with your resume. Run: python -m jobagent accel-outreach
"""
import json
import logging
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from . import db, emailverify, outreach, yc_careers, yc_outreach
from .config import DATA, env
from .filters import AI_TERMS
from .resolve import _registrable

log = logging.getLogger("agent")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36", "Accept-Language": "en"}
ACCELS = {
    "seedcamp": "https://seedcamp.com/companies/",
    "techstars": "https://www.techstars.com/portfolio",
    "antler": "https://www.antler.co/portfolio",
    "founders-factory": "https://foundersfactory.com/portfolio/",
}
SKIP_HOST = re.compile(r"linkedin|twitter|x\.com|facebook|instagram|youtube|google|apple|medium|github|crunchbase|notion|typeform|calendly|substack|wikipedia|"
                       r"cookie|privacy|hubspot|wordpress|cloudflare|eventbrite|spotify|vimeo|tiktok|forms\.|mailto|bit\.ly|t\.co|apps\.apple|play\.google", re.I)
CACHE = DATA / "accel"


def portfolio(accel: str, url: str) -> list[dict]:
    """[{'accel', 'name', 'domain'}] from one portfolio page; cached for a week."""
    CACHE.mkdir(exist_ok=True)
    f = CACHE / f"{accel}.json"
    if f.exists() and datetime.fromtimestamp(f.stat().st_mtime) > datetime.now() - timedelta(days=7):
        return json.loads(f.read_text(encoding="utf-8"))
    try:
        soup = BeautifulSoup(httpx.get(url, headers=UA, timeout=40, follow_redirects=True).text, "html.parser")
    except Exception:
        return []
    own = urlparse(url).netloc.lower().removeprefix("www.")
    seen, out = set(), []
    for a in soup.select("a[href^=http]"):
        host = urlparse(a["href"]).netloc.lower().removeprefix("www.")
        if not host or own.split(".")[0] in host or SKIP_HOST.search(host):
            continue
        dom = _registrable(host)
        if dom in seen:
            continue
        seen.add(dom)
        img = a.find("img")
        name = (a.get("title") or a.get_text(" ", strip=True) or (img.get("alt") if img else "") or dom.split(".")[0]).strip()
        name = re.sub(r"^(link to|visit|go to|read more about)\s+", "", re.sub(r"\s+", " ", name), flags=re.I).strip()
        name = re.sub(r"(?i)[’']s (website|site|homepage)$|\s+(website|homepage)$", "", name).strip()
        name = name[:60] if 2 <= len(name) <= 60 else dom.split(".")[0].title()
        out.append({"accel": accel, "name": name, "domain": dom})
    f.write_text(json.dumps(out), encoding="utf-8")
    return out


def _cached(name: str, build) -> list[dict]:
    CACHE.mkdir(exist_ok=True)
    f = CACHE / f"{name}.json"
    if f.exists() and datetime.fromtimestamp(f.stat().st_mtime) > datetime.now() - timedelta(days=3):
        return json.loads(f.read_text(encoding="utf-8"))
    try:
        out = build()
    except Exception as e:
        log.info("%s portfolio fetch failed: %s", name, e)
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
    if out:
        f.write_text(json.dumps(out), encoding="utf-8")
    return out


def _speedrun() -> list[dict]:
    """a16z Speedrun companies (newest cohort first): the list page's embedded data, then each company page for its website."""
    r = httpx.get("https://speedrun.a16z.com/companies", headers=UA, timeout=40, follow_redirects=True)
    data = json.loads(re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S).group(1))
    build = data["buildId"]
    rows = sorted(data["props"]["pageProps"]["companies"]["results"], key=lambda c: c.get("cohort") or "", reverse=True)
    out = []
    for c in rows:
        try:
            d = httpx.get(f"https://speedrun.a16z.com/_next/data/{build}/companies/{c['slug']}.json", headers=UA, timeout=30).json()["pageProps"]["company"]
            host = urlparse(d.get("website_url") or "").netloc.lower().removeprefix("www.")
            founders = [{"name": f"{f.get('first_name', '')} {f.get('last_name', '')}".strip(), "title": f.get("title") or "Founder"} for f in d.get("founder_set", [])]
        except Exception:
            continue
        if host and not SKIP_HOST.search(host):
            out.append({"accel": "a16z-speedrun", "name": c["name"][:60], "domain": _registrable(host), "founders": founders})
        time.sleep(0.3)
    return out


def _a16z() -> list[dict]:
    """a16z portfolio (the page carries the whole list in one data attribute): active, non-exited companies funded since 2022, newest first."""
    t = httpx.get("https://a16z.com/portfolio/", headers=UA, timeout=90, follow_redirects=True).text
    el = BeautifulSoup(t, "html.parser").select_one("[data-companies]")
    out = []
    for it in json.loads(el["data-companies"]):
        host = urlparse(it.get("external_url") or it.get("url") or "").netloc.lower().removeprefix("www.")
        funded = (it.get("initial_a16z_date_funded") or "")[:10]
        if not host or SKIP_HOST.search(host) or it.get("status") == "Exits" or not it.get("name") or funded < env("A16Z_SINCE", "2024-01-01"):
            continue
        out.append({"accel": "a16z", "name": it["name"][:60], "domain": _registrable(host), "funded": funded})
    out.sort(key=lambda c: c["funded"], reverse=True)
    return out


def _peakxv() -> list[dict]:
    """Peak XV Partners portfolio (peakxv.com/our-companies): one page per company, whose first non-social external link is the company website."""
    base = "https://www.peakxv.com"
    soup = BeautifulSoup(httpx.get(base + "/our-companies", headers=UA, timeout=40, follow_redirects=True).text, "html.parser")
    slugs = list(dict.fromkeys(a["href"] for a in soup.select("a[href^='/companies/']")))
    out = []
    for href in slugs:
        try:
            page = BeautifulSoup(httpx.get(base + href, headers=UA, timeout=25, follow_redirects=True).text, "html.parser")
        except Exception:
            continue
        site = next((a["href"] for a in page.select("a[href^=http]")
                     if "peakxv" not in a["href"] and not SKIP_HOST.search(urlparse(a["href"]).netloc)), None)
        if not site:
            continue
        host = urlparse(site).netloc.lower().removeprefix("www.")
        h1 = page.select_one("h1")
        name = (h1.get_text(" ", strip=True) if h1 else href.rsplit("/", 1)[-1].replace("-", " ").title())[:60]
        out.append({"accel": "peak-xv", "name": name, "domain": _registrable(host)})
        time.sleep(0.25)
    return out


def all_companies() -> list[dict]:
    """Every portfolio, interleaved round-robin so each pass mixes early-stage companies (a16z Speedrun, newest a16z investments) with
    established ones (Peak XV, older a16z portfolio, Seedcamp, Techstars...) instead of finishing one list before starting the next."""
    since = env("A16Z_SINCE", "2024-01-01")  # a16z companies funded from 2024 on (the cached list may be older)
    a16z = [c for c in _cached("a16z", _a16z) if (c.get("funded") or "") >= since]
    lists = [_cached("a16z-speedrun", _speedrun), a16z, _cached("peak-xv", _peakxv)]
    lists += [portfolio(accel, url) for accel, url in ACCELS.items()]
    out, seen = [], set()
    for i in range(max((len(x) for x in lists), default=0)):
        for lst in lists:
            if i < len(lst) and lst[i]["domain"] not in seen:
                seen.add(lst[i]["domain"])
                out.append(lst[i])
    return out


def _homepage(domain: str) -> str:
    for u in (f"https://{domain}", f"https://www.{domain}"):
        try:
            r = httpx.get(u, headers=UA, timeout=20, follow_redirects=True)
            if r.status_code == 200:
                s = BeautifulSoup(r.text, "html.parser")
                return re.sub(r"\s+", " ", (s.title.get_text() if s.title else "") + " " + s.get_text(" ", strip=True))[:3500]
        except Exception:
            continue
    return ""


def _careers_job(c, accel: str, name: str, domain: str, text: str) -> bool:
    """Second channel: an application on the company's own careers page / hiring board, queued like any other ready job."""
    jid = "accelcareers:" + domain
    if c.execute("SELECT 1 FROM jobs WHERE id=?", (jid,)).fetchone():
        return False
    c.commit()
    url, title, how = yc_careers.careers_for_domain(name, domain)
    if not url:
        return False
    db.upsert(c, {"id": jid, "source": "accel_careers", "title": title, "company": name, "location": "see company", "url": url,
                  "description": f"{text} [{accel} portfolio company]", "posted_at": None, "applicants": None})
    c.execute("UPDATE jobs SET status='ready', notes=? WHERE id=?", (f"careers-page application ({how}); {accel} portfolio", jid))
    c.commit()
    return True


def run(limit: int = 5) -> dict:
    c = db.conn()
    out = {"careers_queued": 0, "looked_at": 0, "not_ai": 0, "no_founder": 0, "emails_sent": 0, "no_address": 0}
    done = {r[0] for r in c.execute("SELECT id FROM jobs WHERE id LIKE 'accel:%'")}
    for comp in all_companies():
        if out["looked_at"] >= limit:
            break
        jid = "accel:" + comp["domain"]
        if jid in done:
            continue
        out["looked_at"] += 1
        text = _homepage(comp["domain"])
        job = {"id": jid, "source": "accel", "title": "AI Engineer (open application)", "company": comp["name"], "location": "see company",
               "url": "https://" + comp["domain"], "description": f"{text} [{comp['accel']} portfolio company]", "posted_at": None, "applicants": None}
        db.upsert(c, job)
        c.commit()  # never hold the database write lock across slow network calls (careers lookup, address probing)
        if len(AI_TERMS.findall(text)) < 3:
            out["not_ai"] += 1
            c.execute("UPDATE jobs SET status='skipped', reason='accelerator company is not AI-focused' WHERE id=?", (jid,))
            c.commit()
            continue
        out["careers_queued"] += _careers_job(c, comp["accel"], comp["name"], comp["domain"], text)
        contacts = [x for x in outreach.find_contacts(comp["name"], comp["name"], comp["domain"], {**job, "notes": "", "_founders": comp.get("founders")}, c) if x.get("first_name")][:2]
        sent = []
        for ct in contacts:
            first, last = outreach._split(ct.get("name") or ct["first_name"])
            email, how = ct.get("email"), "found"
            if not email:
                addr, v = emailverify.find(first, last, comp["domain"])
                if v == "verified":
                    email, how = addr, "verified"
                elif v == "unverifiable" and addr and ct is contacts[0] and yc_outreach._guess_allowed(c):
                    email, how = addr, "guess"
            if not email:
                continue
            email = email.lower()
            if c.execute("SELECT 1 FROM outreach WHERE email=?", (email,)).fetchone():
                continue
            subject, body = outreach.compose(first, comp["name"], outreach.DEFAULT_FOCUS)
            c.execute("INSERT OR IGNORE INTO outreach(company, display_company, domain, job_id, name, first_name, title, email, source, confidence, status, subject, body, notes) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (comp["name"], comp["name"], comp["domain"], jid, ct.get("name") or first, first, ct.get("title") or "Founder", email, "accel-founder", "high", "drafted",
                       subject, body, f"{comp['accel']} portfolio ({how})"))
            c.commit()
            oid = c.execute("SELECT id FROM outreach WHERE email=?", (email,)).fetchone()["id"]
            ok, why = outreach.send_row(c, oid)
            if ok:
                sent.append(f"{ct.get('name') or first} <{email}>")
            elif "cap" in why:
                break
        if sent:
            out["emails_sent"] += len(sent)
            c.execute("UPDATE jobs SET status='applied', applied_at=datetime('now'), notes=? WHERE id=?", (f"open application: emailed {comp['accel']} founder(s): " + "; ".join(sent), jid))
        else:
            out["no_founder" if not contacts else "no_address"] += 1
            c.execute("UPDATE jobs SET status='needs_input', notes=? WHERE id=?", (f"{comp['accel']} company, AI-focused; " + ("no founders found on its site" if not contacts else "no verifiable address"), jid))
        c.commit()
        time.sleep(1)
    for r in c.execute("SELECT id, company, description FROM jobs WHERE source='accel' AND status IN ('applied','needs_input') AND COALESCE(notes,'') NOT LIKE '%[careers checked]%' AND id NOT IN "
                       "(SELECT 'accel:' || substr(id, 14) FROM jobs WHERE id LIKE 'accelcareers:%') LIMIT ?", (limit,)).fetchall():
        dom = r["id"].removeprefix("accel:")
        out["careers_queued"] += _careers_job(c, "accelerator", r["company"], dom, (r["description"] or "")[:3000])  # companies handled before careers pages existed
        c.execute("UPDATE jobs SET notes=COALESCE(notes,'') || ' [careers checked]' WHERE id=? AND COALESCE(notes,'') NOT LIKE '%[careers checked]%'", (r["id"],))
        c.commit()
    log.info("accelerator outreach: %s", out)
    return out
