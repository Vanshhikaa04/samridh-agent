"""Dutch visa sponsors (IND-recognised, via indsponsors.nl) -> their open AI/ML jobs.

1. sync():      pull the sponsor companies that fit our rules (tech/finance/health sectors, up to ~200 employees).
2. discover():  for each sponsor, look on the job-board systems startups use (Ashby, Greenhouse, Lever, Workable, Recruitee,
                Personio, Teamtailor, SmartRecruiters) for AI/ML engineering roles; score them like any other job.
3. is_sponsor(): lets scoring give a bonus to any job (e.g. from LinkedIn) at a recognised sponsor.

The list mirrors the public IND register. It is read in a few paginated calls and cached locally, and job-board lookups are
spread across cycles, so this stays light on every server involved.
"""
import json
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from . import db, filters
from .config import DATA, env, search_cfg
from .resolve import _registrable
from .sources import _iso

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
BASE = "https://wswkvkwehfzzbbkhnagz.supabase.co/rest/v1/rpc/"
AUTH_FILE = DATA / "ind_auth.json"
AI_HINT = re.compile(r"\b(ai|artificial intelligence|machine learning|ml|llm|nlp|data|analytics|software|platform|automation|saas|cloud|cyber|digital|app|product)\b", re.I)
LEGAL = re.compile(r"\b(b\.?v\.?|n\.?v\.?|holding|holdings|group|nederland|netherlands|international|europe|benelux|ltd|limited|inc|gmbh|llc|the)\b", re.I)


def norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", LEGAL.sub(" ", re.sub(r"\(.*?\)", " ", name.lower())))


# ------------------------------------------------------------------ the sponsor list
def _auth() -> dict:
    """The directory's public read key, read from the page itself (cached 3 days) so key rotation never breaks us."""
    if AUTH_FILE.exists():
        d = json.loads(AUTH_FILE.read_text())
        if datetime.fromisoformat(d["at"]) > datetime.now() - timedelta(days=3):
            return d["headers"]
    from playwright.sync_api import sync_playwright
    hdr: dict = {}

    def on_req(r):
        if "supabase.co/rest/v1/rpc/search_companies" in r.url and not hdr:
            hdr.update({k: v for k, v in r.headers.items() if k.lower() in ("apikey", "authorization", "content-profile")})
    with sync_playwright() as pw:
        b = pw.chromium.launch(channel=env("BROWSER_CHANNEL", "msedge"), headless=True)
        p = b.new_page()
        p.on("request", on_req)
        p.goto("https://indsponsors.nl/directory", wait_until="networkidle")
        p.wait_for_timeout(2000)
        b.close()
    hdr["Content-Type"] = "application/json"
    AUTH_FILE.write_text(json.dumps({"at": datetime.now().isoformat(), "headers": hdr}))
    return hdr


def sync(max_rows: int = 6000) -> int:
    """Pull sponsors that fit our rules and store them. Returns how many are stored."""
    cfg = search_cfg().get("sponsors", {})
    sectors = cfg.get("sectors", ["Technology & IT"])
    sizes = cfg.get("sizes", ["1-10", "11-50", "51-200"])
    h = _auth()
    c = db.conn()
    n, offset = 0, 0
    while offset < max_rows:
        r = httpx.post(BASE + "search_companies", headers=h, timeout=60, json={
            "p_search": None, "p_sectors": sectors, "p_provinces": None, "p_cities": None, "p_sizes": sizes, "p_kvk": None,
            "p_newly_added_only": False, "p_max_date_added": None, "p_research_only": False, "p_limit": 1000, "p_offset": offset})
        if r.status_code != 200:
            break
        rows = r.json()
        for x in rows:
            site = x.get("website_url") or ""
            dom = _registrable(urlparse(site if "//" in site else "//" + site).netloc.lower().removeprefix("www.")) if site else None
            desc = x.get("description") or ""
            c.execute("INSERT INTO sponsors(id, name, norm, kvk, city, province, sector, size, website, domain, description, linkedin_url, ai_hint, fetched_at) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now')) ON CONFLICT(id) DO UPDATE SET website=excluded.website, domain=excluded.domain, "
                      "description=excluded.description, size=excluded.size, fetched_at=datetime('now')",
                      (x["id"], x["name"], norm(x["name"]), x.get("kvk_number"), x.get("city"), x.get("province"), x.get("sector"),
                       x.get("employee_count"), site, dom, desc, x.get("linkedin_url"), 1 if AI_HINT.search(desc) else 0))
            n += 1
        c.commit()
        if len(rows) < 1000:
            break
        offset += 1000
        time.sleep(1)
    return n


_NORMS: set[str] | None = None


def is_sponsor(company: str) -> bool:
    """True if this company is on the IND sponsor list we have stored (any size/sector we synced)."""
    global _NORMS
    if _NORMS is None:
        _NORMS = {r["norm"] for r in db.conn().execute("SELECT norm FROM sponsors")}
    return bool(_NORMS) and norm(company) in _NORMS


# ------------------------------------------------------------------ their open jobs
def _get(url: str, **kw):
    try:
        r = httpx.get(url, headers=UA, timeout=8, follow_redirects=True, **kw)
        return r if r.status_code == 200 else None
    except Exception:
        return None


def _page_text(url: str) -> str:
    r = _get(url)
    if not r:
        return ""
    soup = BeautifulSoup(r.text, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" "))[:5000]


def ats_jobs(slug: str) -> list[dict]:
    """Every public job at this company across the ATS systems startups use. Returns dicts: title, location, url, description, posted_at."""
    out: list[dict] = []
    if (r := _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")):
        out += [{"title": j["title"], "location": ("Remote - " if j.get("isRemote") else "") + (j.get("location") or ""), "url": j.get("applyUrl") or j["jobUrl"],
                 "description": j.get("descriptionPlain", ""), "posted_at": _iso(j.get("publishedAt")), "ats": "ashby"} for j in r.json().get("jobs", [])]
    if (r := _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"})):
        out += [{"title": j["title"], "location": (j.get("location") or {}).get("name", ""), "url": j["absolute_url"],
                 "description": BeautifulSoup(j.get("content", ""), "html.parser").get_text(" ")[:5000], "posted_at": _iso(j.get("first_published") or j.get("updated_at")),
                 "ats": "greenhouse"} for j in r.json().get("jobs", [])]
    if (r := _get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})) and isinstance(r.json(), list):
        out += [{"title": j["text"], "location": (j.get("categories") or {}).get("location", ""), "url": j["hostedUrl"],
                 "description": j.get("descriptionPlain", ""), "posted_at": _iso(j.get("createdAt")), "ats": "lever"} for j in r.json()]
    if (r := _get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", params={"details": "true"})):
        out += [{"title": j["title"], "location": ", ".join(x for x in (j.get("city"), j.get("country")) if x), "url": j.get("application_url") or j.get("url") or j.get("shortlink", ""),
                 "description": BeautifulSoup(j.get("description", ""), "html.parser").get_text(" ")[:5000], "posted_at": _iso(j.get("published_on") or j.get("created_at")),
                 "ats": "workable"} for j in r.json().get("jobs", [])]
    if (r := _get(f"https://{slug}.recruitee.com/api/offers")):
        out += [{"title": j["title"], "location": j.get("location", ""), "url": j["careers_url"],
                 "description": BeautifulSoup(j.get("description", ""), "html.parser").get_text(" ")[:5000], "posted_at": _iso(j.get("published_at") or j.get("created_at")),
                 "ats": "recruitee"} for j in r.json().get("offers", [])]
    if (r := _get(f"https://{slug}.jobs.personio.de/xml")):
        try:
            for p in ET.fromstring(r.text).iter("position"):
                desc = " ".join(BeautifulSoup(v.text or "", "html.parser").get_text(" ") for v in p.iter("value"))[:5000]
                out.append({"title": p.findtext("name", ""), "location": p.findtext("office", ""), "url": f"https://{slug}.jobs.personio.de/job/{p.findtext('id')}",
                            "description": desc, "posted_at": _iso(p.findtext("createdAt")), "ats": "personio"})
        except ET.ParseError:
            pass
    if (r := _get(f"https://{slug}.teamtailor.com/jobs.rss")):
        try:
            for it in ET.fromstring(r.text).iter("item"):
                out.append({"title": it.findtext("title", ""), "location": "", "url": it.findtext("link", ""),
                            "description": BeautifulSoup(it.findtext("description", ""), "html.parser").get_text(" ")[:5000],
                            "posted_at": _iso(it.findtext("pubDate")), "ats": "teamtailor"})
        except ET.ParseError:
            pass
    if (r := _get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings")):
        out += [{"title": j["name"], "location": ", ".join(x for x in ((j.get("location") or {}).get("city"), (j.get("location") or {}).get("country")) if x),
                 "url": f"https://jobs.smartrecruiters.com/{slug}/{j['id']}", "description": "", "posted_at": _iso(j.get("releasedDate")), "ats": "smartrecruiters"}
                for j in r.json().get("content", [])]
    return [j for j in out if j["title"] and j["url"]]


def _slugs(row) -> list[str]:
    """Likely board names: the domain label (fentures.nl -> fentures) and the cleaned company name."""
    s = []
    if row["domain"]:
        s.append(row["domain"].split(".")[0].lower())
    nm = re.sub(r"[^a-z0-9]", "", LEGAL.sub(" ", row["name"].lower()))
    nmh = "-".join(re.sub(r"[^a-z0-9 ]", " ", LEGAL.sub(" ", row["name"].lower())).split())
    for x in (nm, nmh):
        if x and x not in s:
            s.append(x)
    return [x for x in s if len(x) >= 3][:3]


def _check(row) -> tuple[str, list[dict]]:
    found: list[dict] = []
    for slug in _slugs(row):
        found += ats_jobs(slug)
        if found:
            break  # the first slug that is a real board wins
    return row["id"], found


def discover(limit: int = 300) -> dict:
    """Check the next `limit` unchecked (or week-old) sponsors, AI-flavoured descriptions first. Adds AI/ML jobs to the pipeline."""
    from .__main__ import score_one
    c = db.conn()
    rows = c.execute("SELECT * FROM sponsors WHERE ats_checked_at IS NULL OR ats_checked_at < datetime('now','-7 days') "
                     "ORDER BY ai_hint DESC, ats_checked_at LIMIT ?", (limit,)).fetchall()
    stats = {"sponsors_checked": 0, "boards_with_jobs": 0, "ai_jobs": 0, "new": 0, "ready": 0}
    by_id = {r["id"]: r for r in rows}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for sid, jobs in ex.map(_check, rows):
            sp = by_id[sid]
            stats["sponsors_checked"] += 1
            c.execute("UPDATE sponsors SET ats_checked_at=datetime('now'), jobs_found=? WHERE id=?", (len(jobs), sid))
            if jobs:
                stats["boards_with_jobs"] += 1
            for j in jobs:
                if not filters.title_ok(j["title"]) or any(x in j["title"].lower() for x in search_cfg()["exclude_title"]):
                    continue
                stats["ai_jobs"] += 1
                job = {"id": f"sponsor:{j['ats']}:{re.sub(r'[^a-z0-9]', '', sp['name'].lower())}:{abs(hash(j['url'])) % 10**9}", "source": "sponsor",
                       "title": j["title"], "company": sp["name"], "location": j["location"] or f"{sp['city']}, Netherlands", "url": j["url"],
                       "description": j["description"] or "", "posted_at": j["posted_at"], "applicants": None}
                if not job["description"]:
                    job["description"] = _page_text(job["url"])
                if not db.upsert(c, job):
                    continue
                stats["new"] += 1
                ok, why = filters.check(job)
                if not ok:
                    c.execute("UPDATE jobs SET status='filtered', reason=? WHERE id=?", (why, job["id"]))
                    c.commit()
                    continue
                c.execute("UPDATE jobs SET status='new', notes=? WHERE id=?", (f"IND-recognised sponsor ({sp['size']} employees, {sp['city']}); {j['ats']} board", job["id"]))
                c.commit()
                if score_one(c, dict(c.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone())) == "ready":
                    stats["ready"] += 1
            c.commit()
    return stats
