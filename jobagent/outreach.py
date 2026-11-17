"""Cold outreach: email the people who hire (CEO / CTO / engineering lead / talent lead) at companies you care about.

Pipeline: companies from your jobs -> company domain -> contacts (job-ad email, Hunter, Apollo, the company's own
team/contact pages) -> your template, personalised -> stored in the `outreach` table (dashboard: /outreach) -> sent from
your Gmail with your resume attached.

Safe by default: emails are written and shown in the dashboard first; they only go out when you press Send there or set
OUTREACH_AUTO_SEND=1. Sending is capped per day and spaced out so your Gmail account is not flagged.
"""
import imaplib
import email as emaillib
import json
import unicodedata
import random
import re
import smtplib
import ssl
import time
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from . import db
from .config import DATA, env, profile, search_cfg
from .llm import chat
from .resolve import AGGREGATORS, ATS_HOSTS, _company_site, _registrable, _rendered, _search

TEMPLATE = DATA / "outreach_template.txt"
DEFAULT_FOCUS = "LLM applications, agents, RAG, evaluation and production AI"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}

GENERIC_LOCALS = {"info", "hello", "hi", "contact", "careers", "career", "jobs", "job", "hr", "recruiting", "recruitment", "talent",
                  "people", "team", "support", "sales", "office", "admin", "press", "mail", "apply", "applications", "karriar", "jobb"}
GENERIC_LOCALS |= {"cto", "ceo", "cfo", "coo", "cio", "cpo", "founder", "founders", "cofounder", "head", "director", "vp", "manager", "recruiter",
                   "hiring", "staffing", "ta", "inbox", "enquiries", "enquiry", "general", "careersteam", "talentteam", "jobsteam"}
SOCIAL = ("facebook.", "twitter.", "x.com", "instagram.", "youtube.", "crunchbase.", "wikipedia.", "bloomberg.", "pitchbook.", "zoominfo.",
          "glassdoor.", "indeed.", "linkedin.", "tiktok.", "medium.", "github.", "ycombinator.", "dnb.", "allabolag.", "proff.", "kompass.",
          "opencorporates.", "owler.", "apollo.", "rocketreach.", "cbinsights.", "tracxn.", "arbetsformedlingen.", "reddit.", "quora.")
LEGAL = re.compile(r"[\s,]+(inc\.?|llc|ltd\.?|limited|gmbh|ab|aktiebolag(et)?|ag|sa|s\.a\.|bv|b\.v\.|plc|corp\.?|corporation|co\.?|oy|aps|sarl|sas|srl|"
                   r"s\.r\.l\.|pte\.?|pty|kk)\.?$", re.I)

# lower number = contact first. Recruiters read applications; engineering leads decide; founders are the last resort.
ROLE_RANK = [
    (re.compile(r"talent|recruit|hiring|head of people|people (ops|operations|partner|lead|&|and)|\bhr\b|human resources", re.I), 1),
    (re.compile(r"head of (engineering|ai|data|ml|machine|technology|product eng)|engineering manager|director of engineering|"
                r"vp (of )?(engineering|ai|data)|lead (ai|ml)|ai lead|ml lead", re.I), 2),
    (re.compile(r"\bcto\b|chief technology|chief (ai|data|technical) officer|technical co-?founder", re.I), 3),
    (re.compile(r"\bceo\b|chief executive|founder|co-?founder|managing director|\bowner\b", re.I), 4),
]


def role_rank(title: str) -> int:
    for rx, rank in ROLE_RANK:
        if rx.search(title or ""):
            return rank
    return 99


# ------------------------------------------------------------------ names & domains
def display_company(company: str, title: str = "") -> str:
    """'Umain AB' -> 'Umain'. If a staffing firm posted the job, use the employer named in the title ('... till Axfood IT')."""
    name = company
    m = re.search(r"\b(?:till|at|for|hos)\s+([A-Z][\w&.\- ]{2,40})$", title or "")
    if m and re.search(r"akademisk|rekryter|staffing|recruit|consult|bemanning|talent|\bants\b", company, re.I):
        name = m.group(1)
    name = re.sub(r"\s*\((publ|pty|inc)[^)]*\)", "", name, flags=re.I)
    prev = None
    while prev != name:
        prev, name = name, LEGAL.sub("", name).strip(" ,.")
    return name or company


def brand(name: str, domain: str | None) -> str:
    """'Spiich Labs' on spiich.ai -> 'Spiich' (the brand people use); 'Titan OS' on titanos.com stays whole."""
    toks = name.split()
    label = (domain or "").split(".")[0].lower()
    if len(toks) > 1 and label and toks[0].lower() == label:
        return toks[0]
    return name


FREE_MAIL = {"gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com", "live.com", "proton.me", "protonmail.com"}


def _mentions(domain: str, display: str) -> bool:
    """True if the domain's homepage actually names this company (stops 'Titan OS' resolving to the Titan watch company)."""
    phrase = " ".join(re.sub(r"[^a-z0-9 ]", " ", display.lower()).split())
    if not phrase:
        return False
    for root in (f"https://{domain}", f"https://www.{domain}"):
        try:
            r = httpx.get(root, headers=UA, timeout=12, follow_redirects=True)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        text = " ".join(re.sub(r"[^a-z0-9 ]", " ", (soup.get_text(" ") + " " + (soup.title.get_text() if soup.title else "")).lower()).split())
        if re.search(rf"\b{re.escape(phrase)}\b", text) or phrase.replace(" ", "") in domain.replace("-", "").replace(".", ""):
            return True
        if len(text) < 300 and phrase.split()[0] in domain:  # JavaScript shell with almost no text
            return True
    return False


def company_domain(c, company: str, display: str, job: dict) -> str | None:
    row = c.execute("SELECT domain FROM companies WHERE name=?", (company,)).fetchone()
    if row and row["domain"]:
        return row["domain"]
    domain = None
    # 1) the job ad's own email address (Swedish ads): filip@spiich.ai -> spiich.ai
    m = re.match(r"jobtech:(\d+)", job["id"])
    if m:
        try:
            mail = (httpx.get(f"https://jobsearch.api.jobtechdev.se/ad/{m.group(1)}", timeout=20).json().get("application_details") or {}).get("email")
            if mail and mail.split("@")[-1].lower() not in FREE_MAIL:
                domain = _registrable(mail.split("@")[-1].lower())
        except Exception:
            pass
    # 2) the job link's own host, when it is the company's site (not an ATS / aggregator / recruiter)
    host = urlparse(job["url"]).netloc.lower().removeprefix("www.")
    if not domain and host and not any(a in host for a in AGGREGATORS + SOCIAL) and not host.endswith(ATS_HOSTS) and "ants.se" not in host:
        cand = _registrable(host)
        domain = cand if _mentions(cand, display) else None
    # 3) the company's LinkedIn page (reliable): built from the LinkedIn job id when we have one
    if not domain:
        li = job["url"] if "linkedin.com" in job["url"] else None
        if not li and job["id"].startswith("linkedin:"):
            li = f"https://www.linkedin.com/jobs/view/{job['id'].split(':')[1]}"
        if not li:
            li = (re.search(r"LinkedIn: (\S+)", job.get("notes") or "") or [None, None])[1]
        site = _company_site(li) if li else None
        domain = _registrable(site) if site else None
    # 4) web search, but only trust a result whose homepage really names the company
    if not domain:
        toks = [t for t in re.split(r"\W+", display.lower()) if len(t) >= 3] or [re.sub(r"\W", "", display.lower())]
        for q in (f"{display} official website", f"{display} company careers"):
            for u in _search(q)[:8]:
                h = urlparse(u).netloc.lower().removeprefix("www.")
                if any(a in h for a in AGGREGATORS + SOCIAL) or h.endswith(ATS_HOSTS):
                    continue
                if any(t in h.replace("-", "") for t in toks) and _mentions(_registrable(h), display):
                    domain = _registrable(h)
                    break
            if domain:
                break
    c.execute("INSERT INTO companies(name, display, domain) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET domain=excluded.domain, display=excluded.display",
              (company, display, domain))
    c.commit()
    return domain


# ------------------------------------------------------------------ contact sources
def _split(name: str) -> tuple[str, str]:
    parts = [p for p in re.split(r"\s+", name.strip()) if p]
    return (parts[0], parts[-1]) if len(parts) > 1 else (parts[0] if parts else "", "")


def from_job_ad(job: dict, domain: str) -> list[dict]:
    """The application address printed in the job ad itself (Swedish ads list it)."""
    m = re.match(r"jobtech:(\d+)", job["id"])
    if not m:
        return []
    try:
        mail = (httpx.get(f"https://jobsearch.api.jobtechdev.se/ad/{m.group(1)}", timeout=20).json().get("application_details") or {}).get("email")
    except Exception:
        return []
    if not mail:
        return []
    local = mail.split("@")[0].lower()
    named = re.fullmatch(r"[a-z]{2,}", local) and local not in GENERIC_LOCALS
    return [{"name": local.title() if named else "", "first_name": local.title() if named else "",
             "title": "Contact named in the job ad" if named else "Careers inbox (from the job ad)",
             "email": mail, "source": "job-ad", "confidence": "high", "rank": 1 if named else 5}]


def hunter_domain(domain: str) -> tuple[list[dict], str | None]:
    """ONE Hunter search: the emails it knows plus the company's address pattern (e.g. '{first}.{last}'), which then covers everyone else."""
    key = env("HUNTER_API_KEY")
    if not key:
        return [], None
    try:
        r = httpx.get("https://api.hunter.io/v2/domain-search", params={"domain": domain, "limit": 100, "api_key": key}, timeout=30)
        if r.status_code != 200:
            return [], None
        data = r.json().get("data") or {}
        out = []
        for e in data.get("emails", []):
            title = e.get("position") or ""
            if role_rank(title) == 99 or not e.get("first_name") or (e.get("verification") or {}).get("status") == "invalid":
                continue
            out.append({"name": f"{e['first_name']} {e.get('last_name') or ''}".strip(), "first_name": e["first_name"], "title": title,
                        "email": e["value"], "source": "hunter", "confidence": "high", "rank": role_rank(title)})
        return out, data.get("pattern")
    except Exception:
        return [], None


def from_apollo(domain: str, max_enrich: int = 3) -> list[dict]:
    key = env("APOLLO_API_KEY")
    if not key:
        return []
    h = {"x-api-key": key.strip(), "Content-Type": "application/json", "accept": "application/json"}
    try:
        resp = httpx.post("https://api.apollo.io/api/v1/mixed_people/api_search", headers=h, timeout=30, json={
            "q_organization_domains_list": [domain], "per_page": 25,
            "person_titles": ["Founder", "Co-Founder", "CEO", "CTO", "Head of Engineering", "Engineering Manager", "Head of AI", "VP Engineering",
                              "Talent Acquisition", "Recruiter", "Head of People", "Hiring Manager"]})
        if resp.status_code != 200:  # say why (bad key, no API access on this plan, out of credits, rate limit) instead of silently finding nobody
            import logging
            logging.getLogger("agent").warning("apollo: HTTP %s for %s: %s", resp.status_code, domain, resp.text[:200])
            return []
        s = resp.json()
        people = sorted((p for p in s.get("people", []) if role_rank(p.get("title") or "") < 99), key=lambda p: role_rank(p.get("title") or ""))
        out = []
        for p in people[:max_enrich]:  # enrichment spends Apollo credits, so only the best few
            r = httpx.post("https://api.apollo.io/api/v1/people/match", headers=h, timeout=30, json={"id": p["id"]}).json().get("person") or {}
            em = r.get("email")
            if em and "@" in em and "not_unlocked" not in em:
                out.append({"name": f"{r.get('first_name', '')} {r.get('last_name', '')}".strip(), "first_name": r.get("first_name", ""),
                            "title": r.get("title") or p.get("title") or "", "email": em, "source": "apollo", "confidence": "high",
                            "rank": role_rank(r.get("title") or p.get("title") or "")})
        return out
    except Exception:
        return []


PATTERNS = ["{first}.{last}", "{first}{last}", "{f}{last}", "{f}.{last}", "{first}.{l}", "{first}{l}", "{first}_{last}", "{first}-{last}",
            "{last}.{first}", "{last}{f}", "{last}", "{first}", "{f}{l}", "{last}.{f}"]


def _norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", s)


def _apply_pattern(pat: str, first: str, last: str) -> str:
    f, l = _norm_name(first), _norm_name(last.split()[-1] if last else "")
    return pat.format(first=f, last=l, f=f[:1], l=l[:1])


def _pattern_of(first: str, last: str, local: str) -> str | None:
    """Which format explains this (name, address)? 'Jane','Doe','jane.d' -> '{first}.{l}'."""
    for pat in PATTERNS:
        if ("{last}" in pat or "{l}" in pat) and not last:
            continue
        if _apply_pattern(pat, first, last) == local.lower():
            return pat
    return None


def pattern_from_pairs(pairs: list[tuple[str, str, str]]) -> str | None:
    """pairs = (first, last, address-local-part). The most common explaining pattern wins."""
    votes: dict[str, int] = {}
    for first, last, local in pairs:
        if first and (pat := _pattern_of(first, last, local)):
            votes[pat] = votes.get(pat, 0) + 1
    return max(votes, key=votes.get) if votes else None


def from_site(domain: str, known: list[dict], known_pattern: str | None = None) -> tuple[list[dict], str | None]:
    """Named leaders + published addresses from the company's own pages; emails only where evidence supports them."""
    base = f"https://{domain}"
    texts, found_mails, extra_pages = [], set(), []
    paths = ["", "/team", "/about", "/about-us", "/company", "/people", "/leadership", "/contact", "/careers", "/founders", "/our-team",
             "/story", "/our-story", "/who-we-are", "/company/team", "/about/team", "/about-us/team", "/meet-the-team", "/the-team"]
    for path in paths:
        if path == "/story" and sum(len(t) for t in texts) > 6000:
            break  # enough text already; the rest are long shots
        for root in (base, f"https://www.{domain}"):
            try:
                r = httpx.get(root + path, headers=UA, timeout=12, follow_redirects=True)
            except Exception:
                continue
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            found_mails |= {m.lower() for m in re.findall(rf"[\w.+-]+@{re.escape(domain)}", r.text, re.I)}
            texts.append(re.sub(r"\s+", " ", soup.get_text(" "))[:4000])
            if path == "":  # links on the homepage that lead to a team / about page (any path name)
                for a in soup.select("a[href]"):
                    label = a.get_text(" ", strip=True).lower()
                    href = urljoin(str(r.url), a["href"]).split("#")[0]
                    if (len(label) < 30 and re.search(r"^(team|our team|about|about us|company|founders|people|leadership|meet the team|who we are|our story|story)$", label)
                            and domain in urlparse(href).netloc and href not in extra_pages):
                        extra_pages.append(href)
            break
    for href in extra_pages[:4]:
        try:
            rr = httpx.get(href, headers=UA, timeout=12, follow_redirects=True)
            if rr.status_code == 200:
                found_mails |= {m.lower() for m in re.findall(rf"[\w.+-]+@{re.escape(domain)}", rr.text, re.I)}
                texts.append(re.sub(r"\s+", " ", BeautifulSoup(rr.text, "html.parser").get_text(" "))[:4000])
        except Exception:
            continue
    if sum(len(t) for t in texts) < 800:  # JavaScript-built site: render the team page in a browser
        for path in ("/team", "/about", ""):
            _, txt = _rendered(base + path)
            if txt:
                texts.append(re.sub(r"\s+", " ", txt)[:4000])
    text = " \n".join(texts)[:14000]
    people = []
    if text.strip():
        try:
            raw = chat('Extract named people WITH their job titles from this company website text. Reply JSON {"people":[{"name":"First Last","title":"..."}]}. '
                       "Only real named individuals who appear with a title. Never invent anyone.", text, json_mode=True)
            people = [p for p in json.loads(raw).get("people", []) if len(p.get("name", "").split()) >= 2 and role_rank(p.get("title", "")) < 99]
        except Exception:
            people = []

    # learn the address pattern from real (name, address) evidence: the job-ad contact, published addresses, anything already known
    pairs = [(c["first_name"], c["name"].split()[-1] if len(c["name"].split()) > 1 else "", c["email"].split("@")[0])
             for c in known if c.get("email") and c.get("first_name")]
    for p in people:
        first, last = _split(p["name"])
        pairs += [(first, last, m.split("@")[0]) for m in found_mails]
    pattern = pattern_from_pairs(pairs) or known_pattern
    out = []
    for p in people:
        first, last = _split(p["name"])
        mail, src, conf = None, "site", "medium"
        for m in found_mails:
            local = m.split("@")[0]
            if first.lower() in local and (last.lower() in local or local.lower() == first.lower()):
                mail = m
        if not mail and pattern and last:
            mail, src, conf = f"{_apply_pattern(pattern, first, last)}@{domain}", "pattern", "medium"
        # No evidence for an address: keep the person anyway (blank email) so you can fill it in, instead of guessing.
        out.append({"name": p["name"], "first_name": first, "title": p["title"], "email": mail or "",
                    "source": src if mail else "site (no email published)", "confidence": conf if mail else "none", "rank": role_rank(p["title"])})
    for m in found_mails:  # published generic inboxes like careers@ are fair game for a job enquiry
        local = m.split("@")[0]
        if local in GENERIC_LOCALS and local in ("careers", "career", "jobs", "hr", "recruiting", "talent", "people", "apply", "applications"):
            out.append({"name": "", "first_name": "", "title": "Careers inbox (published on the site)", "email": m, "source": "site", "confidence": "high", "rank": 5})
    return out, pattern


def from_search(company: str, domain: str) -> list[dict]:
    """Founders named in web-search results (LinkedIn titles, news, directories). Only people whose result text names this company or its domain."""
    try:
        r = httpx.post("https://html.duckduckgo.com/html/", data={"q": f'"{company}" {domain} founder OR CEO OR co-founder'}, headers=UA, timeout=25)
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception:
        return []
    key = re.compile(r"(?<![a-z0-9])" + re.escape(company.lower()) + r"(?![a-z0-9])")
    hits = []
    for a, sn in zip(soup.select("a.result__a"), soup.select(".result__snippet")):
        t = (a.get_text(" ", strip=True) + " | " + sn.get_text(" ", strip=True))
        if key.search(t.lower()) or domain in t.lower():
            hits.append(t[:300])
    if not hits:
        return []
    try:
        raw = chat(f"Search results about the company '{company}' (website {domain}). Extract named people who are founders, co-founders, CEO or CTO of THAT company. "
                   'Reply JSON {"people":[{"name":"First Last","title":"..."}]}. Only people the text clearly ties to this company; if a result is about a different '
                   "company or person with a similar name, ignore it. Never invent anyone.", "\n".join(hits), json_mode=True)
        people = [p for p in json.loads(raw).get("people", []) if len(p.get("name", "").split()) >= 2 and role_rank(p.get("title", "")) < 99]
    except Exception:
        return []
    return [{"name": p["name"], "first_name": _split(p["name"])[0], "title": p["title"], "email": "", "source": "web search", "confidence": "none",
             "rank": role_rank(p["title"])} for p in people[:2]]


def hunter_find(domain: str, first: str, last: str) -> str | None:
    """Hunter's per-person email-finder. Costs a credit each, so it is OFF unless OUTREACH_HUNTER_FINDER=1."""
    key = env("HUNTER_API_KEY")
    if not key or not last or env("OUTREACH_HUNTER_FINDER", "0") != "1":
        return None
    try:
        d = httpx.get("https://api.hunter.io/v2/email-finder", params={"domain": domain, "first_name": first, "last_name": last, "api_key": key}, timeout=30).json()
        data = d.get("data") or {}
        return data["email"] if data.get("email") and (data.get("score") or 0) >= 70 else None
    except Exception:
        return None


def find_contacts(company: str, display: str, domain: str, job: dict, c=None) -> list[dict]:
    """Free sources first. One Hunter search at most per company, and only if somebody still lacks an address; the address
    pattern it (or any real address) reveals is saved and applied to every other named person, so it costs nothing more."""
    row = c.execute("SELECT email_pattern, hunter_searched_at FROM companies WHERE name=?", (company,)).fetchone() if c else None
    saved_pattern = row["email_pattern"] if row else None
    cands = from_job_ad(job, domain)
    site, pattern = from_site(domain, [x for x in cands if x["email"]], saved_pattern)
    cands += site
    for f in job.get("_founders") or []:  # founders listed in a directory (e.g. a16z Speedrun): named, titled, no address yet
        nm = (f.get("name") or "").strip()
        if len(nm.split()) >= 2 and not any(x["name"].lower() == nm.lower() for x in cands):
            cands.append({"name": nm, "first_name": _split(nm)[0], "title": f.get("title") or "Founder", "email": "", "source": "directory",
                          "confidence": "none", "rank": role_rank(f.get("title") or "Founder")})
    if not [x for x in cands if x["first_name"]]:  # nobody named on the site: ask the web (search snippets that clearly name this company)
        cands += from_search(display or company, domain)
    pattern = pattern or saved_pattern
    unresolved = [x for x in cands if not x["email"] and x["first_name"]]
    if unresolved and not pattern and env("HUNTER_API_KEY") and not (row and row["hunter_searched_at"]):
        h_emails, h_pattern = hunter_domain(domain)
        if c:
            c.execute("UPDATE companies SET hunter_searched_at=datetime('now') WHERE name=?", (company,))
            c.commit()
        cands += h_emails
        pairs = [(x["first_name"], x["name"].split()[-1] if len(x["name"].split()) > 1 else "", x["email"].split("@")[0]) for x in cands if x["email"] and x["first_name"]]
        pattern = h_pattern or pattern_from_pairs(pairs)
    cands += from_apollo(domain)
    if pattern:  # your rule: same company, same address format
        for x in cands:
            if not x["email"] and x["first_name"]:
                first, last = _split(x["name"])
                if last or pattern == "{first}":
                    x.update(email=f"{_apply_pattern(pattern, first, last)}@{domain}", source="pattern", confidence="medium",
                             notes=f"address built from this company's pattern {pattern}")
        if c:
            c.execute("UPDATE companies SET email_pattern=? WHERE name=?", (pattern, company))
            c.commit()
    conf_order = {"high": 0, "medium": 1, "low": 2, "none": 3}
    seen_mail, picked = set(), []
    for x in sorted(cands, key=lambda x: (0 if x["email"] else 1, x["rank"], conf_order.get(x["confidence"], 0))):
        em = (x["email"] or "").lower()
        if em:
            if em in seen_mail or not re.fullmatch(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", em):
                continue
            seen_mail.add(em)
        elif any(x["name"].lower() == y["name"].lower() for y in picked):
            continue
        picked.append(x)
    personal = [x for x in picked if x["first_name"]]
    generic = [x for x in picked if not x["first_name"]]
    k = int(env("OUTREACH_PER_COMPANY", "3"))
    chosen = (personal[:k] + generic[:1]) if personal else generic[:1]
    for x in chosen:
        if not x["email"]:
            f, l = _split(x["name"])
            if (found := hunter_find(domain, f, l)):
                x.update(email=found, source="hunter-finder", confidence="high")
    return chosen


# ------------------------------------------------------------------ writing the email
def role_focus(c, company: str, job: dict) -> str:
    # Your template's own wording by default (only the name and company change). OUTREACH_TAILOR_FOCUS=1 rewrites this one
    # phrase from each job posting instead.
    if env("OUTREACH_TAILOR_FOCUS", "0") != "1":
        return DEFAULT_FOCUS
    row = c.execute("SELECT role_focus FROM companies WHERE name=?", (company,)).fetchone()
    if row and row["role_focus"]:
        return row["role_focus"]
    focus = DEFAULT_FOCUS
    if job.get("description"):
        try:
            out = chat("From this job posting, state the role's focus as a short comma-separated phrase (max 12 words) like "
                       f"'{DEFAULT_FOCUS}'. Use only topics the posting actually mentions. Reply with the phrase only, no quotes.",
                       job["description"][:3500]).strip().strip("'\".")
            if 8 <= len(out) <= 110 and "\n" not in out:
                focus = out
        except Exception:
            pass
    c.execute("UPDATE companies SET role_focus=? WHERE name=?", (focus, company))
    c.commit()
    return focus


def compose(first_name: str, company: str, focus: str) -> tuple[str, str]:
    body = TEMPLATE.read_text(encoding="utf-8").replace("{company}", company).replace("{role_focus}", focus)
    body = body.replace("Hey {first_name},", f"Hey {first_name}," if first_name else "Hello,")
    return f"AI Engineer opportunity at {company} - {profile()['name']}", body


# ------------------------------------------------------------------ pipeline
def _skip_company(name: str) -> bool:
    cfg = search_cfg()
    n = name.lower()
    return any(b in n for b in cfg.get("big_company_blocklist", [])) or any(h in n for h in cfg.get("agency_hints", []))


def targets(c) -> list[dict]:
    rows = c.execute("SELECT * FROM jobs WHERE status IN ('applied','interview','offer') "
                     "ORDER BY (status='applied') DESC, score DESC").fetchall()
    seen, out = set(), []
    for r in rows:
        if r["company"] in seen or _skip_company(r["company"]):
            continue
        seen.add(r["company"])
        out.append(dict(r))
    return out


def add_company(c, company: str, domain: str):
    """Manually add a target: python -m jobagent outreach-add 'Company' domain.com"""
    c.execute("INSERT INTO companies(name, display, domain) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET domain=excluded.domain",
              (company, display_company(company), domain))
    c.commit()
    job = {"id": f"manual:{company}", "url": f"https://{domain}", "notes": "", "description": "", "company": company, "title": "AI Engineer"}
    return _store(c, company, job, domain)


def _store(c, company: str, job: dict, domain: str) -> int:
    disp = brand(display_company(company, job.get("title", "")), domain)
    focus = role_focus(c, company, job)
    n = 0
    for ct in find_contacts(company, disp, domain, job, c):
        subject, body = compose(ct["first_name"], disp, focus)
        if c.execute("SELECT 1 FROM outreach WHERE company=? AND name=? AND name!=''", (company, ct["name"])).fetchone():
            continue  # this person is already on the list
        cur = c.execute("INSERT OR IGNORE INTO outreach(company, display_company, domain, job_id, name, first_name, title, email, source, "
                        "confidence, status, subject, body, notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (company, disp, domain, job["id"], ct["name"], ct["first_name"], ct["title"], ct["email"].lower() or None, ct["source"],
                         ct["confidence"], "drafted" if ct["email"] else "needs_email", subject, body, ct.get("notes", "")))
        n += cur.rowcount
    c.execute("UPDATE companies SET contacts_searched_at=datetime('now') WHERE name=?", (company,))
    c.commit()
    return n


def fill_from_patterns(c) -> int:
    """Your rule: once ONE real address at a company is known, build everyone else's the same way (jane.d@x.com -> john.s@x.com).
    Upgrades 'needs_email' people whenever a pattern is known, at no API cost."""
    done = 0
    for r in c.execute("SELECT * FROM outreach WHERE status='needs_email'").fetchall():
        co = c.execute("SELECT email_pattern FROM companies WHERE name=?", (r["company"],)).fetchone()
        pattern = co["email_pattern"] if co else None
        if not pattern:  # learn it from any real address already on this company's list
            pairs = [(x["first_name"], x["name"].split()[-1] if len(x["name"].split()) > 1 else "", x["email"].split("@")[0])
                     for x in c.execute("SELECT first_name, name, email FROM outreach WHERE company=? AND email IS NOT NULL AND email!='' "
                                        "AND source NOT IN ('pattern')", (r["company"],))]
            pattern = pattern_from_pairs(pairs)
            if pattern:
                c.execute("UPDATE companies SET email_pattern=? WHERE name=?", (pattern, r["company"]))
        if not pattern:
            continue
        first, last = _split(r["name"])
        if not (last or pattern == "{first}"):
            continue
        mail = f"{_apply_pattern(pattern, first, last)}@{r['domain']}".lower()
        if c.execute("SELECT 1 FROM outreach WHERE email=?", (mail,)).fetchone():
            continue
        c.execute("UPDATE outreach SET email=?, source='pattern', confidence='medium', status='drafted', notes=? WHERE id=?",
                  (mail, f"address built from this company's pattern {pattern}", r["id"]))
        done += 1
    c.commit()
    return done


def sent_today(c) -> int:
    from . import caps
    return c.execute("SELECT COUNT(*) FROM outreach WHERE date(sent_at)=date('now') AND sent_at > ?", (caps.since(),)).fetchone()[0]


def smtp_ready() -> bool:
    return bool(env("SMTP_HOST") and env("SMTP_USER") and env("SMTP_PASSWORD"))


def resume_pdf():
    return next(iter(sorted(DATA.glob("*[Rr]esume*.pdf"))), None)


def send_row(c, oid: int) -> tuple[bool, str]:
    r = c.execute("SELECT * FROM outreach WHERE id=?", (oid,)).fetchone()
    if not r:
        return False, "no such row"
    if r["status"] not in ("drafted", "failed"):
        return False, f"already {r['status']}"
    if not smtp_ready():
        return False, "SMTP not configured: add SMTP_HOST / SMTP_USER / SMTP_PASSWORD (a Gmail app password) to .env"
    if sent_today(c) >= int(env("OUTREACH_MAX_PER_DAY", "15")):
        return False, f"daily cap of {env('OUTREACH_MAX_PER_DAY', '15')} reached (protects your Gmail account)"
    # Bounces ("550 5.4.1 recipient address rejected") hurt the Gmail sender's reputation. Address probing is blocked from this network, so:
    # never send to addresses that were only GUESSED from a name pattern (about a third of those bounced), and skip domains with no mail server.
    if r["source"] == "pattern" and env("OUTREACH_SEND_GUESSED", "0") != "1":
        c.execute("UPDATE outreach SET status='skipped', notes='guessed address (name pattern): not sent, would likely bounce' WHERE id=?", (oid,))
        c.commit()
        return False, "guessed address skipped"
    try:
        from . import emailverify
        if not emailverify.mx_hosts((r["email"] or "").split("@")[-1].lower()) and not emailverify._resolves((r["email"] or "").split("@")[-1].lower()):
            c.execute("UPDATE outreach SET status='skipped', notes='domain has no mail server: not sent' WHERE id=?", (oid,))
            c.commit()
            return False, "domain has no mail server"
    except Exception:
        pass  # a DNS hiccup must not stop real mail
    if c.execute("UPDATE outreach SET status='sending' WHERE id=? AND status IN ('drafted','failed')", (oid,)).rowcount == 0:
        return False, "another process is already sending this one"
    c.commit()
    pdf = resume_pdf()
    try:
        from . import tailor as _t
        _j = c.execute("SELECT title FROM jobs WHERE id=?", (r["job_id"],)).fetchone() if r["job_id"] else None
        pdf = _t.base_resume(dict(_j)) if _j else pdf
    except Exception:
        pass
    tailored = c.execute("SELECT path FROM resumes WHERE job_id=?", (r["job_id"],)).fetchone() if r["job_id"] else None
    if tailored and Path(tailored["path"]).exists():  # the version tailored to the job this person is hiring for
        pdf = Path(tailored["path"])
    if not pdf:
        c.execute("UPDATE outreach SET status='drafted' WHERE id=?", (oid,))
        c.commit()
        return False, "no resume PDF in data/"
    msg = EmailMessage()
    msg["To"], msg["From"], msg["Subject"] = r["email"], env("SMTP_FROM") or profile()["email"], r["subject"]
    msg.set_content(r["body"])
    msg.add_attachment(pdf.read_bytes(), maintype="application", subtype="pdf", filename=pdf.name)
    try:
        port = int(env("SMTP_PORT", "465"))
        if port == 465:
            with smtplib.SMTP_SSL(env("SMTP_HOST"), port, context=ssl.create_default_context(), timeout=30) as s:
                s.login(env("SMTP_USER"), env("SMTP_PASSWORD")); s.send_message(msg)
        else:
            with smtplib.SMTP(env("SMTP_HOST"), port, timeout=30) as s:
                s.starttls(context=ssl.create_default_context()); s.login(env("SMTP_USER"), env("SMTP_PASSWORD")); s.send_message(msg)
    except Exception as e:
        c.execute("UPDATE outreach SET status='failed', notes=? WHERE id=?", (f"{type(e).__name__}: {str(e)[:150]}", oid))
        c.commit()
        return False, f"SMTP error: {type(e).__name__}: {str(e)[:120]}"
    c.execute("UPDATE outreach SET status='sent', sent_at=datetime('now'), notes='sent with resume attached' WHERE id=?", (oid,))
    c.commit()
    return True, "sent"


def regenerate(c, oid: int):
    """After you edit a name/email in the dashboard, rewrite that email's text."""
    r = c.execute("SELECT * FROM outreach WHERE id=?", (oid,)).fetchone()
    focus = (c.execute("SELECT role_focus FROM companies WHERE name=?", (r["company"],)).fetchone() or {"role_focus": None})["role_focus"] or DEFAULT_FOCUS
    subject, body = compose(r["first_name"], r["display_company"], focus)
    c.execute("UPDATE outreach SET subject=?, body=? WHERE id=?", (subject, body, oid))
    c.commit()


def send_queue(c) -> int:
    """Auto-send mode: send drafted emails, best contacts first, spaced out."""
    sent = 0
    for r in c.execute("SELECT id FROM outreach WHERE status='drafted' AND confidence NOT IN ('low','none') "
                       "AND (company IN (SELECT company FROM jobs WHERE status IN ('applied','interview','offer')) OR source='resend') ORDER BY "
                       "CASE source WHEN 'job-ad' THEN 0 WHEN 'apollo' THEN 1 WHEN 'hunter' THEN 1 ELSE 2 END, id").fetchall():
        ok, why = send_row(c, r["id"])
        if not ok:
            if "cap" in why or "SMTP not configured" in why:
                break
            continue
        sent += 1
        time.sleep(random.uniform(45, 120))
    return sent


def run_cycle(auto_send: bool | None = None) -> dict:
    c = db.conn()
    found = 0
    searched = {r["name"] for r in c.execute("SELECT name FROM companies WHERE contacts_searched_at > datetime('now','-14 days')")}
    for job in targets(c):
        if job["company"] in searched:
            continue
        disp = display_company(job["company"], job["title"])
        domain = company_domain(c, job["company"], disp, job)
        if not domain:
            continue
        found += _store(c, job["company"], job, domain)
    found += hr_fallback(c)
    found += fill_from_patterns(c)
    auto = env("OUTREACH_AUTO_SEND", "0") == "1" if auto_send is None else auto_send
    sent = send_queue(c) if auto else 0
    return {"new_drafts": found, "sent": sent, "auto_send": auto, "smtp": smtp_ready()}


# ------------------------------------------------------------------ people you type in by hand
EMAIL_RX = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def parse_entries(text: str) -> list[dict]:
    """Lines like  'jane@acme.io'  /  'jane@acme.io, Jane, Acme'  /  'Jane Doe <jane@acme.io>'  /  several addresses on one line."""
    out = []
    for line in re.split(r"[\n;]+", text or ""):
        line = line.strip()
        if not line:
            continue
        found = EMAIL_RX.findall(line)
        leftover = EMAIL_RX.sub("", line)
        if len(found) > 1 and not re.search(r"[A-Za-z]", leftover):  # a plain list of addresses (space/comma separated)
            out += [{"email": e.lower(), "first_name": "", "company": ""} for e in found]
            continue
        if not found:
            out.append({"email": "", "raw": line, "first_name": "", "company": ""})
            continue
        email = found[0]
        front = re.match(r"^\s*([^<,@]+?)\s*<", line)  # "Jane Doe <jane@acme.io>"
        fields = [x.strip() for x in leftover.replace("<", " ").replace(">", " ").split(",")]  # positional: blanks keep their place
        if fields and not fields[0]:
            fields = fields[1:]  # the text before the first comma was only the address
        if front:
            first = front.group(1).split()[0]
            company = next((f for f in fields if f and f != front.group(1).strip()), "")
        else:
            first = fields[0].split()[0] if fields and fields[0] else ""
            company = fields[1] if len(fields) > 1 else ""
        out.append({"email": email.lower(), "first_name": first.title() if first.islower() else first, "company": company})
    return out


def guess_first_name(email: str) -> str:
    """'filip@spiich.ai' -> 'Filip', 'jane.doe@acme.io' -> 'Jane'. Generic inboxes ('careers@') give '' so the email starts 'Hello,'."""
    local = email.split("@")[0].lower()
    first = re.split(r"[._-]", local)[0]
    return first.title() if re.fullmatch(r"[a-z]{2,}", first) and first not in GENERIC_LOCALS else ""


def company_from_email(c, email: str) -> str:
    """Company name for the template: from your sponsor/company lists by domain, else the domain's brand ('acme-labs.io' -> 'Acme Labs')."""
    reg = _registrable(email.split("@")[-1].lower())
    row = c.execute("SELECT name FROM sponsors WHERE domain=? LIMIT 1", (reg,)).fetchone()
    if row:
        return brand(display_company(row["name"]), reg)
    row = c.execute("SELECT display FROM companies WHERE domain=? LIMIT 1", (reg,)).fetchone()
    if row and row["display"]:
        return row["display"]
    return " ".join(w.capitalize() for w in re.split(r"[-_]", reg.split(".")[0]) if w) or reg


def add_manual(c, entries: list[dict]) -> list[dict]:
    """Create (or reuse) an outreach row for each typed-in person. Returns one result per entry; sending is done by send_row."""
    results = []
    for e in entries:
        email = (e.get("email") or "").strip().lower()
        if not email or not EMAIL_RX.fullmatch(email):
            results.append({"input": e.get("raw") or email or "(empty)", "ok": False, "message": "not a valid email address"})
            continue
        first = e.get("first_name") or guess_first_name(email)
        company = (e.get("company") or "").strip() or company_from_email(c, email)
        subject, body = compose(first, company, DEFAULT_FOCUS)
        row = c.execute("SELECT id, status, sent_at FROM outreach WHERE email=?", (email,)).fetchone()
        if row and row["status"] in ("sent", "replied", "bounced", "sending"):
            when = f" on {row['sent_at'][:10]}" if row["sent_at"] else ""
            results.append({"input": email, "ok": False, "id": row["id"], "message": f"already {row['status']}{when}"})
            continue
        if row:  # a draft/skipped/failed row for this person: refresh it with what you typed
            c.execute("UPDATE outreach SET first_name=?, display_company=?, subject=?, body=?, status='drafted' WHERE id=?",
                      (first, company, subject, body, row["id"]))
            oid = row["id"]
        else:
            c.execute("INSERT INTO outreach(company, display_company, domain, job_id, name, first_name, title, email, source, confidence, status, subject, body, notes) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (company, company, email.split("@")[-1], f"manual:{email}", first, first, "Added by you", email, "added by you", "high", "drafted", subject, body, ""))
            oid = c.execute("SELECT id FROM outreach WHERE email=?", (email,)).fetchone()["id"]
        c.commit()
        results.append({"input": email, "ok": True, "id": oid, "first_name": first, "company": company, "message": "ready"})
    return results


def send_manual(ids: list[int]) -> None:
    """Send the typed-in emails one after another (a short pause between them)."""
    c = db.conn()
    for k, oid in enumerate(ids):
        send_row(c, oid)
        if k < len(ids) - 1:
            time.sleep(random.uniform(15, 35))


# ------------------------------------------------------------------ follow-ups
DEFAULT_FOLLOWUP = ("I wanted to follow up on my earlier note about the AI engineering role at {company}. I'm still very interested and would be glad to "
                    "share more about my work or jump on a short call whenever it suits you.")


def _ensure_followups(c) -> None:
    c.execute("CREATE TABLE IF NOT EXISTS followups(id INTEGER PRIMARY KEY AUTOINCREMENT, outreach_id INTEGER, sent_at TEXT, subject TEXT, body TEXT, "
              "status TEXT, note TEXT)")
    c.commit()


def followups_today(c) -> int:
    _ensure_followups(c)
    return c.execute("SELECT COUNT(*) FROM followups WHERE status='sent' AND date(sent_at)=date('now')").fetchone()[0]


def followup_candidates(c, days: int = 5) -> list[dict]:
    """Emails that went out at least `days` days ago, got no reply, and have not been followed up within the last `days` days."""
    _ensure_followups(c)
    rows = c.execute(
        "SELECT o.*, (SELECT COUNT(*) FROM followups f WHERE f.outreach_id=o.id AND f.status='sent') AS fcount, "
        "(SELECT MAX(f.sent_at) FROM followups f WHERE f.outreach_id=o.id AND f.status='sent') AS flast "
        "FROM outreach o WHERE o.status='sent' AND o.sent_at <= datetime('now', ?) "
        "AND o.company NOT IN (SELECT company FROM outreach WHERE status='replied') "
        "ORDER BY o.sent_at", (f"-{int(days)} days",)).fetchall()
    out = []
    for r in rows:
        if r["flast"] and r["flast"] > (datetime.utcnow() - timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S"):
            continue
        out.append(dict(r))
    return out


def superimpose(message: str, first_name: str, company: str) -> str:
    """Lay your typed message over one recipient: {first_name} / {company} are filled in; a greeting, a mention of the company and a sign-off
    are added only where your text has none. Empty message = the built-in follow-up."""
    msg = (message or "").strip() or DEFAULT_FOLLOWUP
    msg = msg.replace("{first_name}", first_name or "").replace("{company}", company)
    if not re.match(r"(?i)\s*(hey|hi|hello|dear|good (morning|afternoon|evening))\b", msg):
        msg = (f"Hey {first_name}," if first_name else "Hello,") + "\n\n" + msg
    if company.lower() not in msg.lower():  # make sure the reader knows which note this follows
        head, _, rest = msg.partition("\n\n")
        msg = f"{head}\n\nFollowing up on my earlier note about the AI engineering role at {company}.\n\n{rest}".rstrip()
    name = profile()["name"]
    if name.lower() not in msg.lower()[-200:]:
        msg += f"\n\nBest,\n{name}"
    return msg


def _smtp_deliver(msg) -> None:
    port = int(env("SMTP_PORT", "465"))
    if port == 465:
        with smtplib.SMTP_SSL(env("SMTP_HOST"), port, context=ssl.create_default_context(), timeout=30) as s:
            s.login(env("SMTP_USER"), env("SMTP_PASSWORD")); s.send_message(msg)
    else:
        with smtplib.SMTP(env("SMTP_HOST"), port, timeout=30) as s:
            s.starttls(context=ssl.create_default_context()); s.login(env("SMTP_USER"), env("SMTP_PASSWORD")); s.send_message(msg)


def send_followup(c, oid: int, message: str, attach: bool = False) -> tuple[bool, str]:
    _ensure_followups(c)
    r = c.execute("SELECT * FROM outreach WHERE id=?", (oid,)).fetchone()
    if not r or r["status"] != "sent":
        return False, "not a sent email"
    if not smtp_ready():
        return False, "SMTP not configured"
    cap = int(env("OUTREACH_MAX_PER_DAY", "15"))
    if sent_today(c) + followups_today(c) >= cap:
        return False, f"daily cap of {cap} reached (new emails + follow-ups), protects your Gmail account"
    subject = r["subject"] if r["subject"].lower().startswith("re:") else "Re: " + r["subject"]
    body = superimpose(message, r["first_name"] or "", r["display_company"] or r["company"])
    em = EmailMessage()
    em["To"], em["From"], em["Subject"] = r["email"], env("SMTP_FROM") or profile()["email"], subject
    em.set_content(body)
    if attach and (pdf := resume_pdf()):
        em.add_attachment(pdf.read_bytes(), maintype="application", subtype="pdf", filename=pdf.name)
    try:
        _smtp_deliver(em)
    except Exception as e:
        c.execute("INSERT INTO followups(outreach_id, sent_at, subject, body, status, note) VALUES(?,datetime('now'),?,?,?,?)",
                  (oid, subject, body, "failed", f"{type(e).__name__}: {str(e)[:150]}"))
        c.commit()
        return False, f"SMTP error: {type(e).__name__}: {str(e)[:120]}"
    c.execute("INSERT INTO followups(outreach_id, sent_at, subject, body, status, note) VALUES(?,datetime('now'),?,?,?,?)", (oid, subject, body, "sent", ""))
    c.commit()
    return True, "sent"


def send_followups(ids: list[int], message: str, attach: bool = False) -> None:
    """Background: one follow-up after another with a pause, stopping at the daily cap."""
    c = db.conn()
    for k, oid in enumerate(ids):
        ok, why = send_followup(c, oid, message, attach)
        if not ok and "cap" in why:
            break
        if k < len(ids) - 1:
            time.sleep(random.uniform(20, 45))


def check_replies(c) -> dict:
    """Mark contacts who wrote back (or whose address bounced) by reading your Gmail over IMAP."""
    if not smtp_ready():
        return {"checked": 0, "note": "needs SMTP_USER / SMTP_PASSWORD"}
    replied = bounced = 0
    try:
        m = imaplib.IMAP4_SSL("imap.gmail.com", timeout=60)
        m.login(env("SMTP_USER"), env("SMTP_PASSWORD"))
        m.select('"[Gmail]/All Mail"', readonly=True)
        for r in c.execute("SELECT id, email, sent_at FROM outreach WHERE status='sent'").fetchall():
            since = datetime.fromisoformat(r["sent_at"]).strftime("%d-%b-%Y")
            typ, d = m.search(None, f'(FROM "{r["email"]}" SINCE {since})')
            if d and d[0].split():
                c.execute("UPDATE outreach SET status='replied', replied_at=datetime('now') WHERE id=?", (r["id"],))
                replied += 1
                continue
            typ, d = m.search(None, f'(FROM "mailer-daemon" SINCE {since} TEXT "{r["email"]}")')
            if d and d[0].split():
                c.execute("UPDATE outreach SET status='bounced', notes='delivery failed (bounce received)' WHERE id=?", (r["id"],))
                bounced += 1
        c.commit()
        m.logout()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:100]}"}
    return {"replied": replied, "bounced": bounced}


# ------------------------------------------------------------------ HR inboxes published on the company's own site
HR_WORDS = re.compile(r"hr|career|job|talent|recruit|people|hiring|apply|application|werk|karriere|rekryt|carri|emploi|jobb|bewerb|personal", re.I)
GENERIC_OK = re.compile(r"^(info|contact|hello|hi|office|mail|team|enquir\w*|kontakt|post|admin)$", re.I)
SITE_PATHS = ("/careers", "/career", "/jobs", "/join-us", "/work-with-us", "/contact", "/contact-us", "/about", "/kontakt", "/karriere", "/karriar", "/impressum", "/legal", "/privacy")


def site_inboxes(domain: str) -> list[str]:
    """Addresses at this domain that the company itself publishes, best-for-hiring first. Nothing is guessed."""
    found: dict[str, int] = {}
    for root in (f"https://{domain}", f"https://www.{domain}"):
        hit_root = False
        for path in ("",) + SITE_PATHS:
            try:
                r = httpx.get(root + path, headers=UA, timeout=10, follow_redirects=True)
            except Exception:
                continue
            if r.status_code != 200:
                continue
            hit_root = True
            text = r.text.replace("&#64;", "@").replace("[at]", "@").replace(" (at) ", "@")
            for m in re.findall(rf"[\w.+-]+@{re.escape(domain)}", text, re.I):
                found[m.lower()] = found.get(m.lower(), 0) + 1
        if hit_root:
            break
    ranked = []
    for mail in found:
        local = mail.split("@")[0]
        if HR_WORDS.search(local):
            ranked.append((0, mail))
        elif GENERIC_OK.match(local):
            ranked.append((1, mail))
    return [m for _, m in sorted(ranked)]


def hr_fallback(c, limit: int = 40) -> int:
    """Applied companies that still have no email address on the list: use an HR/careers inbox the company publishes."""
    added = 0
    for job in targets(c):
        if added >= limit:
            break
        co = job["company"]
        have = c.execute("SELECT 1 FROM outreach WHERE (company=? OR display_company=?) AND email IS NOT NULL AND email!=''", (co, co)).fetchone()
        tried = c.execute("SELECT hr_tried_at FROM companies WHERE name=?", (co,)).fetchone()
        if have or (tried and tried["hr_tried_at"]):
            continue
        disp = display_company(co, job["title"])
        domain = company_domain(c, co, disp, job)
        if not domain:
            continue
        c.execute("INSERT OR IGNORE INTO companies(name, display, domain) VALUES(?,?,?)", (co, disp, domain))
        mails = site_inboxes(domain)
        c.execute("UPDATE companies SET hr_tried_at=datetime('now') WHERE name=?", (co,))
        c.commit()
        if not mails:
            continue
        name = brand(disp, domain)
        subject, body = compose("", name, role_focus(c, co, job))
        mail = mails[0]
        if c.execute("SELECT 1 FROM outreach WHERE email=?", (mail,)).fetchone():
            continue
        cur = c.execute("INSERT OR IGNORE INTO outreach(company, display_company, domain, job_id, name, first_name, title, email, source, confidence, status, subject, body, notes) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (co, name, domain, job["id"], "", "", "HR / careers inbox", mail, "company site", "medium", "drafted", subject, body,
                         "published on the company's own website"))
        added += cur.rowcount
        c.commit()
    return added
