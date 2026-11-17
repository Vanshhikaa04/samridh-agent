"""Job discovery. Each fetcher returns dicts with the jobs-table fields (incl. posted_at as ISO-8601 UTC)."""
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from .config import DATA, env, search_cfg

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}


def _clean(html: str) -> str:
    return re.sub(r"\s+", " ", BeautifulSoup(html or "", "html.parser").get_text(" ")).strip()


def _iso(v) -> str | None:
    """Epoch (s or ms), ISO string or RFC-822 string -> ISO UTC."""
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)) or str(v).isdigit():
            n = float(v)
            return datetime.fromtimestamp(n / 1000 if n > 1e11 else n, timezone.utc).isoformat()
        s = str(v).replace("Z", "+00:00").replace(" ", "T", 1) if re.match(r"\d{4}-\d\d-\d\d", str(v)) else None
        d = datetime.fromisoformat(s) if s else parsedate_to_datetime(str(v))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def _title_match(title: str) -> bool:
    from .filters import title_ok
    return title_ok(title)


def _get(url, **kw):
    r = httpx.get(url, headers=UA, timeout=30, follow_redirects=True, **kw)
    r.raise_for_status()
    return r


# ---- aggregator APIs -------------------------------------------------------------------------

def arbeitnow(pages: int = 3) -> list[dict]:
    out = []
    for p in range(1, pages + 1):
        for j in _get("https://www.arbeitnow.com/api/job-board-api", params={"page": p}).json().get("data", []):
            out.append({"id": f"arbeitnow:{j['slug']}", "source": "arbeitnow", "title": j["title"], "company": j["company_name"],
                        "location": ("Remote - " if j.get("remote") else "") + (j.get("location") or ""), "url": j["url"],
                        "description": _clean(j.get("description", "")), "posted_at": _iso(j.get("created_at"))})
    return out


def remotive(search: str = "ai engineer") -> list[dict]:
    jobs = _get("https://remotive.com/api/remote-jobs", params={"search": search, "limit": 100}).json().get("jobs", [])
    return [{"id": f"remotive:{j['id']}", "source": "remotive", "title": j["title"], "company": j["company_name"],
             "location": "Remote - " + j.get("candidate_required_location", ""), "url": j["url"],
             "description": _clean(j.get("description", "")), "posted_at": _iso(j.get("publication_date"))} for j in jobs]


def remoteok() -> list[dict]:
    data = _get("https://remoteok.com/api").json()
    return [{"id": f"remoteok:{j['id']}", "source": "remoteok", "title": j.get("position", ""), "company": j.get("company", ""),
             "location": "Remote - " + (j.get("location") or "Worldwide"), "url": j.get("url") or j.get("apply_url", ""),
             "description": _clean(j.get("description", "")), "posted_at": _iso(j.get("epoch") or j.get("date"))}
            for j in data if isinstance(j, dict) and j.get("id") and _title_match(j.get("position", ""))]


def jobtech_sweden(query: str = "ai engineer") -> list[dict]:
    out = []
    for j in _get("https://jobsearch.api.jobtechdev.se/search", params={"q": query, "limit": 100}).json().get("hits", []):
        addr = j.get("workplace_address") or {}
        out.append({"id": f"jobtech:{j['id']}", "source": "jobtech", "title": j.get("headline", ""),
                    "company": (j.get("employer") or {}).get("name", ""), "location": f"{addr.get('municipality') or ''}, Sweden",
                    "url": (j.get("application_details") or {}).get("url") or j.get("webpage_url", ""),
                    "description": (j.get("description") or {}).get("text", ""), "posted_at": _iso(j.get("publication_date"))})
    return out


def himalayas() -> list[dict]:
    jobs = _get("https://himalayas.app/jobs/api", params={"limit": 100}).json().get("jobs", [])
    return [{"id": f"himalayas:{j.get('guid') or j.get('applicationLink')}", "source": "himalayas", "title": j.get("title", ""),
             "company": j.get("companyName", ""), "location": "Remote - " + ", ".join(j.get("locationRestrictions") or ["worldwide"]),
             "url": j.get("applicationLink", ""), "description": _clean(j.get("description", "")),
             "posted_at": _iso(j.get("pubDate"))} for j in jobs]


# ---- startup ATS boards (public job-board APIs) ----------------------------------------------

def _pretty(slug: str) -> str:
    return slug.replace("-", " ").title()


def ats_boards() -> list[dict]:
    out = []
    boards = search_cfg().get("ats_boards", {})
    for slug in boards.get("ashby", []):
        try:
            for j in _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}").json().get("jobs", []):
                if _title_match(j["title"]):
                    out.append({"id": f"ashby:{slug}:{j['id']}", "source": "ashby", "title": j["title"], "company": _pretty(slug),
                                "location": ("Remote - " if j.get("isRemote") else "") + (j.get("location") or ""),
                                "url": j.get("applyUrl") or j["jobUrl"], "description": j.get("descriptionPlain", ""),
                                "posted_at": _iso(j.get("publishedAt"))})
        except Exception:
            continue
    for slug in boards.get("greenhouse", []):
        try:
            for j in _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"}).json().get("jobs", []):
                if _title_match(j["title"]):
                    out.append({"id": f"greenhouse:{slug}:{j['id']}", "source": "greenhouse", "title": j["title"], "company": _pretty(slug),
                                "location": (j.get("location") or {}).get("name", ""), "url": j["absolute_url"],
                                "description": _clean(BeautifulSoup(j.get("content", ""), "html.parser").get_text()),
                                "posted_at": _iso(j.get("first_published") or j.get("updated_at"))})
        except Exception:
            continue
    for slug in boards.get("lever", []):
        try:
            for j in _get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"}).json():
                if _title_match(j["text"]):
                    out.append({"id": f"lever:{slug}:{j['id']}", "source": "lever", "title": j["text"], "company": _pretty(slug),
                                "location": (j.get("categories") or {}).get("location", ""), "url": j["hostedUrl"],
                                "description": j.get("descriptionPlain", ""), "posted_at": _iso(j.get("createdAt"))})
        except Exception:
            continue
    return out


# ---- LinkedIn (public guest pages, no login) --------------------------------------------------
# Discovery + applicant-count filter only. Polite: ~1.5s between requests, capped detail fetches, stop on 429.
# Easy Apply needs your login, so those jobs are listed in the dashboard for manual apply.

LI_SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
LI_DETAIL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{}"


def _external_apply_url(html: str) -> str | None:
    m = re.search(r'id="applyUrl"[^>]*>\s*<!--\s*"?([^"\s]+)', html)
    if not m:
        return None
    qs = parse_qs(urlparse(m.group(1).replace("&amp;", "&")).query)
    return unquote(qs["url"][0]) if "url" in qs else None


class RateLimited(Exception):
    """LinkedIn answered 429; stop hitting it this cycle."""


def linkedin_cards(keyword: str, location: str, start: int = 0, entry_only: bool = True) -> list[dict]:
    """One page of public search results (no login). entry_only adds LinkedIn's Entry/Associate filter. Raises RateLimited on 429."""
    f_tpr = f"r{int(search_cfg()['max_age_days']) * 86400}"
    params = {"keywords": keyword, "location": location, "f_TPR": f_tpr, "start": start}
    if entry_only:
        params["f_E"] = entry_only if isinstance(entry_only, str) else "2,3"  # a string picks a level, e.g. "1" = Internship
    r = httpx.get(LI_SEARCH, headers=UA, timeout=30, params=params)
    if r.status_code == 429:
        raise RateLimited()
    if r.status_code != 200:
        return []
    out = []
    for li in BeautifulSoup(r.text, "html.parser").select("li"):
        div = li.select_one("[data-entity-urn]")
        title, comp, place, link = (li.select_one(s) for s in (".base-search-card__title", ".base-search-card__subtitle",
                                                               ".job-search-card__location", "a.base-card__full-link"))
        if not (div and title and comp and link):
            continue
        jid = div["data-entity-urn"].split(":")[-1]
        t = li.select_one("time")
        out.append({"id": f"linkedin:{jid}", "source": "linkedin", "title": title.get_text(strip=True),
                    "company": comp.get_text(strip=True), "location": place.get_text(strip=True) if place else "",
                    "url": link["href"].split("?")[0], "description": "", "applicants": None,
                    "posted_at": _iso(t["datetime"]) if t and t.has_attr("datetime") else None})
    return out


def linkedin_detail(card: dict) -> dict | None:
    """Fill in description + applicant count for one job. Raises RateLimited on 429."""
    d = httpx.get(LI_DETAIL.format(card["id"].split(":")[1]), headers=UA, timeout=30)
    if d.status_code == 429:
        raise RateLimited()
    if d.status_code != 200:
        return None
    soup = BeautifulSoup(d.text, "html.parser")
    cap = soup.select_one(".num-applicants__caption")
    num = re.search(r"(\d[\d,]*)", cap.get_text()) if cap else None
    desc = soup.select_one(".show-more-less-html__markup")
    sal = soup.select_one(".salary, .compensation__salary, .salary-main-rail__data-body, .compensation__salary-range")
    desc_text = (desc.get_text(" ", strip=True) if desc else "") + (f" [Pay: {sal.get_text(' ', strip=True)}]" if sal else "")
    return {**card, "description": desc_text,
            "applicants": int(num.group(1).replace(",", "")) if num else None,
            "url": _external_apply_url(d.text) or card["url"]}


# LinkedIn is driven country-by-country by hunt.py, so it is not part of the plain source list.
def weworkremotely() -> list[dict]:
    """We Work Remotely RSS feeds (programming + everything else; the AI ones are picked out by the title filter)."""
    out = []
    for feed in ("remote-programming-jobs", "remote-jobs", "remote-devops-sysadmin-jobs", "remote-full-stack-programming-jobs", "remote-back-end-programming-jobs"):
        try:
            xml = _get(f"https://weworkremotely.com/categories/{feed}.rss").text if feed != "remote-jobs" else _get("https://weworkremotely.com/remote-jobs.rss").text
        except Exception:
            continue
        for it in re.findall(r"<item>(.*?)</item>", xml, re.S):
            def tag(name):
                m = re.search(rf"<{name}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{name}>", it, re.S)
                return m.group(1).strip() if m else ""
            raw_title, link = tag("title"), tag("link")
            if not link or ":" not in raw_title:
                continue
            company, title = [x.strip() for x in raw_title.split(":", 1)]
            out.append({"id": "wwr:" + re.sub(r"\W+", "-", link.split("/")[-1])[:80], "source": "wwr", "title": title.replace("&amp;", "&"),
                        "company": company.replace("&amp;", "&"), "location": "Remote - " + (tag("region") or "Worldwide"), "url": link,
                        "description": _clean(tag("description")), "posted_at": _iso(tag("pubDate"))})
    return out


WELLFOUND_ROLES = ["machine-learning-engineer", "ai-engineer", "data-scientist", "intern"]
WELLFOUND_PLACES = ["india", "united-kingdom", "germany", "netherlands", "sweden", "ireland", "portugal", "spain", "france", "poland", "denmark",
                    "finland", "norway", "estonia", "lithuania", "switzerland", "austria", "belgium", "canada", "united-states", "australia",
                    "singapore", "united-arab-emirates"]


def wellfound() -> list[dict]:
    """Wellfound (startup jobs): role + location pages carry their listings in the page data (title, pay, company size, description)."""
    import json as _json
    seen, out = set(), []
    allpages = [f"https://wellfound.com/role/l/{r}/{p}" for r in WELLFOUND_ROLES for p in WELLFOUND_PLACES] + \
               [f"https://wellfound.com/role/r/{r}" for r in WELLFOUND_ROLES]
    # Wellfound blocks fast crawlers (HTTP 429 "Security Check"): a few pages per cycle, with pauses, rotating through every role/place.
    state = DATA / "wellfound_state.json"
    try:
        idx = int(_json.loads(state.read_text())["idx"]) % len(allpages)
    except Exception:
        idx = 0
    per_cycle = 8
    pages = [allpages[(idx + k) % len(allpages)] for k in range(per_cycle)]
    for k, url in enumerate(pages):
        if k:
            time.sleep(7)
        try:
            html = _get(url).text
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                break  # blocked for now: keep what we have, try again next cycle
            continue
        except Exception:
            continue
        state.write_text(_json.dumps({"idx": (idx + k + 1) % len(allpages)}))
        try:
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
            data = _json.loads(m.group(1))["props"]["pageProps"]["apolloState"]["data"]
        except Exception:
            continue
        owner = {}  # job ref -> startup record
        for v in data.values():
            if isinstance(v, dict) and v.get("__typename") == "StartupResult":
                for ref in v.get("highlightedJobListings") or []:
                    owner[ref.get("__ref")] = v
        for key, v in data.items():
            if not (isinstance(v, dict) and v.get("__typename") == "JobListingSearchResult") or v["id"] in seen:
                continue
            seen.add(v["id"])
            co = owner.get(key, {})
            size = (co.get("companySize") or "").replace("SIZE_", "").replace("_", "-")
            places = ", ".join(v.get("locationNames") or []) or ("Remote" if v.get("remote") else "")
            desc = _clean(v.get("description") or "") + f" [Compensation: {v.get('compensation') or 'not stated'}] [Company size: {size or '?'}] [{co.get('highConcept', '')}]"
            out.append({"id": f"wellfound:{v['id']}", "source": "wellfound", "title": v.get("title") or v.get("primaryRoleTitle", ""),
                        "company": co.get("name") or "?", "location": ("Remote - " if v.get("remote") else "") + places,
                        "url": f"https://wellfound.com/jobs/{v['id']}-{v.get('slug', '')}", "description": desc, "posted_at": _iso(v.get("liveStartAt"))})
    return out


def hn_hiring() -> list[dict]:
    """Hacker News 'Ask HN: Who is hiring?' (this month and last): every top-level comment is one company's posting. Startups, often no big-company
    noise. The apply target is the first link in the post, or an email address when that is all it gives (applied to by email)."""
    import html as _html
    out = []
    try:
        hits = _get("https://hn.algolia.com/api/v1/search_by_date", params={"tags": "story,author_whoishiring", "hitsPerPage": 6}).json()["hits"]
    except Exception:
        return out
    threads = [h for h in hits if "who is hiring" in h["title"].lower()][:2]
    for th in threads:
        try:
            kids = _get(f"https://hn.algolia.com/api/v1/items/{th['objectID']}").json().get("children", [])
        except Exception:
            continue
        for k in kids:
            raw = k.get("text") or ""
            if not raw or k.get("author") is None:
                continue
            txt = _html.unescape(re.sub(r"<[^>]+>", " ", raw.replace("<p>", "\n").replace("<br>", "\n")))
            header = next((ln.strip() for ln in txt.split("\n") if ln.strip()), "")[:200]
            links = [u.rstrip(".,);]") for u in re.findall(r"https?://[^\s\"<>]+", _html.unescape(raw))]
            links = [u for u in links if "news.ycombinator.com" not in u]
            emails = re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", txt)
            segs = [s.strip() for s in re.split(r"\s[|—–]\s|\s-\s|\|", header) if s.strip()]
            company = re.sub(r"\(.*?\)", "", segs[0]).strip()[:60] if segs else "?"
            title = header  # the header lists the roles; the title filter looks for an AI/ML term + an engineering role in it
            loc = next((s for s in segs[1:] if re.search(r"remote|onsite|hybrid|[A-Z][a-z]+,? [A-Z]{2}\b|europe|london|berlin|usa|us\b|uk\b", s, re.I)), "")
            free = re.compile(r"@(gmail|googlemail|yahoo|outlook|hotmail|live|proton(mail)?|icloud|aol|gmx|mail)\.", re.I)
            if not links and emails and all(free.search(e) for e in emails):  # no company site, just a personal inbox: not something to email a CV and phone number to
                continue
            if re.search(r"dm for proposal|grant proposals?|upfront (setup|consulting) fee|setup/consulting fee|pay to (apply|join)|revenue share only|unpaid equity only", txt, re.I):
                continue
            url = links[0] if links else (f"mailto:{emails[0]}" if emails else f"https://news.ycombinator.com/item?id={k['id']}")
            out.append({"id": f"hn:{k['id']}", "source": "hn", "title": title, "company": company or "?", "location": loc[:80] or "see posting",
                        "url": url, "description": txt[:6000], "posted_at": _iso(k.get("created_at"))})
    return out


YC_ROLES = ["machine-learning-engineer", "data-scientist", "software-engineer", "full-stack-engineer", "backend-engineer", "intern", "science", "product-manager-ai"]


def yc_jobs() -> list[dict]:
    """Y Combinator job board (startups only). The role pages list jobs; only AI/ML-titled ones get their detail page fetched.
    Applying needs a YC account (Logins page: 'yc')."""
    out, seen = [], set()
    for role in YC_ROLES:
        try:
            soup = BeautifulSoup(_get(f"https://www.ycombinator.com/jobs/role/{role}").text, "html.parser")
        except Exception:
            continue
        for a in soup.select('a[href*="/companies/"][href*="/jobs/"]'):
            href = a["href"].split("?")[0]
            title = a.get_text(" ", strip=True)
            if href in seen or not (_title_match(title) or re.search(r'engineer|developer|scientist|researcher|founding|intern', title, re.I)):
                continue
            seen.add(href)
            url = href if href.startswith("http") else "https://www.ycombinator.com" + href
            try:
                page = _get(url).text
            except Exception:
                continue
            ps = BeautifulSoup(page, "html.parser")
            t = (ps.title.get_text() if ps.title else title).replace("| Y Combinator", "").strip()
            company = t.split(" at ", 1)[1].strip() if " at " in t else href.split("/")[2].replace("-", " ").title()
            main = ps.select_one("main") or ps
            body = re.sub(r"\s+", " ", main.get_text(" ", strip=True))
            loc = re.search(r"(Remote[^|]{0,40}|[A-Z][a-z]+(?: [A-Z][a-z]+)?, [A-Z]{2}(?:, [A-Z]{2,3})?|London[^|]{0,20}|Berlin[^|]{0,20}|Paris[^|]{0,20})", body)
            out.append({"id": "yc:" + href.split("/jobs/")[1][:40], "source": "yc", "title": title, "company": company, "location": loc.group(1).strip() if loc else "see posting",
                        "url": url, "description": body[:6000] + " [Y Combinator company]", "posted_at": None})
    return out


def hn_jobs() -> list[dict]:
    """news.ycombinator.com/jobs: jobs posted by YC companies ("Acme (YC W24) Is Hiring ML Engineers"), a few pages deep."""
    out, seen, url = [], set(), "https://news.ycombinator.com/jobs"
    for _ in range(5):
        try:
            soup = BeautifulSoup(_get(url).text, "html.parser")
        except Exception:
            break
        for row in soup.select("tr.athing"):
            a = row.select_one("span.titleline > a")
            if not a or row.get("id") in seen:
                continue
            seen.add(row.get("id"))
            title = a.get_text(" ", strip=True)
            href = a["href"] if a["href"].startswith("http") else "https://news.ycombinator.com/" + a["href"]
            sub = row.find_next_sibling("tr")
            age = sub.select_one("span.age") if sub else None
            company = re.split(r"\s*\(YC|\s+[Ii]s [Hh]iring|\s+[Ii]s [Ll]ooking|\s+\|", title)[0].strip()[:60] or "?"
            desc = title
            try:  # the posting itself, for scoring
                page = BeautifulSoup(_get(href).text, "html.parser")
                desc = title + ". " + re.sub(r"\s+", " ", page.get_text(" ", strip=True))[:4000]
            except Exception:
                pass
            out.append({"id": "hnjobs:" + str(row.get("id")), "source": "hn_jobs", "title": title, "company": company, "location": "see posting",
                        "url": href, "description": desc + " [Y Combinator company, from news.ycombinator.com/jobs]",
                        "posted_at": _iso(age["title"].split(" ")[0]) if age and age.get("title") else None})
        more = soup.select_one("a.morelink")
        if not more:
            break
        url = "https://news.ycombinator.com/" + more["href"]
    return out


def _yc_batch_year_ok(batch: str | None) -> bool:
    """Only companies from the YC batches in range (YC_MIN_YEAR..YC_MAX_YEAR, default 2024..2026, i.e. Y24 to Y26)."""
    m = re.search(r"(\d{4})", batch or "")
    return bool(m) and int(env("YC_MIN_YEAR", "2024")) <= int(m.group(1)) <= int(env("YC_MAX_YEAR", "2026"))


def yc_hiring() -> list[dict]:
    """Every YC company that is hiring (public list of ~1,500), AI-focused ones first. Each company's YC page lists its open roles; a few dozen
    companies per cycle, rotating through the list, so the whole thing is covered over a few hours without hammering YC."""
    import html as _h
    import json as _json
    from .filters import AI_TERMS
    try:
        comps = _get("https://yc-oss.github.io/api/companies/hiring.json").json()
    except Exception:
        return []

    def ai_score(c):
        text = " ".join([" ".join(c.get("tags") or []), c.get("one_liner") or "", (c.get("long_description") or "")[:1500]])
        return len(AI_TERMS.findall(text)) + (3 if any("artificial" in t.lower() or "machine learning" in t.lower() or "generative" in t.lower() for t in (c.get("tags") or [])) else 0)

    def remote_rank(c):  # 0 = fully remote, 1 = remote-friendly, 2 = on-site / hybrid only
        regs = c.get("regions") or []
        return 0 if "Fully Remote" in regs else 1 if "Remote" in regs else 2

    comps = [c for c in comps if _yc_batch_year_ok(c.get("batch"))]
    if env("YC_ANY_ENGINEER", "1") != "1":  # default: any YC company hiring engineers, not only the AI-focused ones
        comps = [c for c in comps if ai_score(c) >= 3]
    if env("YC_REMOTE_ONLY", "1") == "1":  # you asked for remote roles: skip companies that are on-site only
        comps = [c for c in comps if remote_rank(c) <= 1]
    comps.sort(key=lambda c: (remote_rank(c), c.get("slug", "")))
    state = DATA / "yc_hiring_state.json"
    try:
        idx = int(_json.loads(state.read_text())["idx"]) % max(1, len(comps))
    except Exception:
        idx = 0
    per_cycle = 30
    batch = [comps[(idx + k) % len(comps)] for k in range(min(per_cycle, len(comps)))]
    state.write_text(_json.dumps({"idx": (idx + per_cycle) % max(1, len(comps))}))
    out = []
    for c in batch:
        try:
            html = _get(f"https://www.ycombinator.com/companies/{c['slug']}").text
            m = re.search(r'data-page="([^"]+)"', html)
            data = _json.loads(_h.unescape(m.group(1)))["props"]
        except Exception:
            continue
        time.sleep(1.2)
        n0 = len(out)
        for jp in data.get("jobPostings") or []:
            title = jp.get("title") or ""
            if not re.search(r"engineer|developer|scientist|researcher|founding|intern|machine learning|\bai\b|\bml\b", title, re.I):
                continue
            exp = jp.get("minExperience") or ""
            rem = "Remote" if (remote_rank(c) == 0 or "remote" in (jp.get("location") or "").lower()) else ""
            desc = (f"{rem and '[REMOTE] '}{c.get('one_liner') or ''}. {(c.get('long_description') or '')[:2500]} [Role: {jp.get('prettyRole')}, {jp.get('type')}] "
                    f"[Minimum experience: {exp}] [Visa: {jp.get('visa') or 'n/a'}] [Y Combinator {c.get('batch') or ''} company]")
            out.append({"id": "yc:" + (jp.get("url") or "").split("/jobs/")[-1][:40], "source": "yc", "title": title, "company": c.get("name") or "?",
                        "location": ("Remote - " if rem and "remote" not in (jp.get("location") or "").lower() else "") + (jp.get("location") or c.get("all_locations") or "see posting"),
                        "url": "https://www.ycombinator.com" + (jp.get("url") or ""), "description": desc, "posted_at": None})
        if len(out) == n0:  # no matching open role: an open application to the founders still goes out
            out.append({"id": "yc:open:" + c["slug"], "source": "yc", "title": "AI Engineer (open application)", "company": c.get("name") or "?",
                        "location": ("Remote - " if remote_rank(c) == 0 else "") + (c.get("all_locations") or "see company"), "url": "https://www.ycombinator.com/companies/" + c["slug"],
                        "description": f"{c.get('one_liner') or ''}. {(c.get('long_description') or '')[:2500]} [No open role listed: open application] [Y Combinator {c.get('batch') or ''} company]",
                        "posted_at": None})
    return out


def yc_recent() -> list[dict]:
    """AI-focused companies from the most recent YC batches, whether or not they list a role: an open application to the founders."""
    import json as _json
    from .filters import AI_TERMS
    try:
        comps = _get("https://yc-oss.github.io/api/companies/all.json").json()
    except Exception:
        return []

    def batch_key(b):  # "Winter 2026" -> sortable
        m = re.match(r"(Winter|Spring|Summer|Fall|Unspecified)\s*(\d{4})", b or "")
        return (int(m.group(2)), {"Winter": 1, "Spring": 2, "Summer": 3, "Fall": 4}.get(m.group(1), 0)) if m else (0, 0)

    batches = sorted({c.get("batch") for c in comps if _yc_batch_year_ok(c.get("batch"))}, key=batch_key, reverse=True)  # every batch from Y24 to Y26

    def ai_score(c):
        text = " ".join([" ".join(c.get("tags") or []), c.get("one_liner") or "", (c.get("long_description") or "")[:1500]])
        return len(AI_TERMS.findall(text)) + (3 if any(("artificial" in t.lower() or "machine learning" in t.lower() or "generative" in t.lower()) for t in (c.get("tags") or [])) else 0)

    def remote_rank(c):
        regs = c.get("regions") or []
        return 0 if "Fully Remote" in regs else 1 if "Remote" in regs else 2

    pool = [c for c in comps if c.get("batch") in batches and c.get("status") != "Inactive" and ai_score(c) >= 3 and c.get("website")]
    pool.sort(key=lambda c: (remote_rank(c), c.get("slug", "")))
    state = DATA / "yc_recent_state.json"
    try:
        idx = int(_json.loads(state.read_text())["idx"]) % max(1, len(pool))
    except Exception:
        idx = 0
    per_cycle = int(env("YC_RECENT_PER_CYCLE", "30"))
    state.write_text(_json.dumps({"idx": (idx + per_cycle) % max(1, len(pool))}))
    out = []
    for c in [pool[(idx + k) % len(pool)] for k in range(min(per_cycle, len(pool)))]:
        out.append({"id": "yc:open:" + c["slug"], "source": "yc", "title": "AI Engineer (open application)", "company": c.get("name") or "?",
                    "location": ("Remote - " if remote_rank(c) == 0 else "") + (c.get("all_locations") or "see company"),
                    "url": "https://www.ycombinator.com/companies/" + c["slug"],
                    "description": f"{c.get('one_liner') or ''}. {(c.get('long_description') or '')[:2500]} [Open application] [Y Combinator {c.get('batch') or ''} company]",
                    "posted_at": None})
    return out


STEALTH_KEYWORDS = ["stealth AI startup", "stealth startup AI engineer", "stealth mode AI startup", "stealth startup founding engineer", "stealth AI"]
STEALTH_PLACES = ["United States", "United Kingdom", "Germany", "Netherlands", "France", "Spain", "Italy", "Sweden", "Canada", "India", "Singapore",
                  "United Arab Emirates", "Switzerland", "Ireland", "Remote"]


def stealth_jobs() -> list[dict]:
    """Jobs from companies in stealth mode (they name themselves 'Stealth Startup', 'Stealth AI Startup' ...), found on LinkedIn. A few keyword/place
    searches per cycle, rotating through all of them, so LinkedIn's rate limit is respected."""
    import json as _json
    from .hunt import _patient
    combos = [(k, p) for p in STEALTH_PLACES for k in STEALTH_KEYWORDS]
    state = DATA / "stealth_state.json"
    try:
        idx = int(_json.loads(state.read_text())["idx"]) % len(combos)
    except Exception:
        idx = 0
    per_cycle = 5
    state.write_text(_json.dumps({"idx": (idx + per_cycle) % len(combos)}))
    found = {}
    for k in range(per_cycle):
        kw, place = combos[(idx + k) % len(combos)]
        try:
            cards = _patient(linkedin_cards, kw, place, 0, False)
        except Exception:
            break
        for card in cards or []:
            if re.search(r"stealth", card["company"] + " " + card["title"], re.I):
                found[card["id"]] = card
        time.sleep(3)
    out = []
    for card in found.values():
        try:
            detail = _patient(linkedin_detail, card)
        except Exception:
            detail = None
        time.sleep(3)
        if detail:
            detail["source"] = "stealth"
            detail["description"] = (detail.get("description") or "") + " [Startup in stealth mode, found on LinkedIn]"
            out.append(detail)
    return out


def remotive_intern() -> list[dict]:
    seen, out = set(), []
    for q in ("ai intern", "machine learning intern", "data science intern", "intern"):
        for j in remotive(q):
            if j["id"] not in seen:
                seen.add(j["id"])
                out.append(j)
    return out


def jobtech_intern() -> list[dict]:
    seen, out = set(), []
    for q in ("ai intern", "machine learning praktik", "ai praktikant", "data science intern"):
        for j in jobtech_sweden(q):
            if j["id"] not in seen:
                seen.add(j["id"])
                out.append(j)
    return out


ALL = {"arbeitnow": arbeitnow, "remotive": remotive, "remoteok": remoteok, "jobtech": jobtech_sweden,
       "himalayas": himalayas, "ats_boards": ats_boards,
       "remotive_intern": remotive_intern, "jobtech_intern": jobtech_intern,
       "weworkremotely": weworkremotely, "wellfound": wellfound, "hn_hiring": hn_hiring, "yc": yc_jobs, "hn_jobs": hn_jobs, "yc_hiring": yc_hiring, "yc_recent": yc_recent, "stealth": stealth_jobs}
