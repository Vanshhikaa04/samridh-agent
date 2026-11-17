"""Find the company's own posting for a LinkedIn job, so we can apply on their site (no LinkedIn login needed).

Strategy: (1) probe public ATS boards using slugs derived from the company name, (2) web-search fallback,
verifying the page heading. Only returns a URL when the title is a close match, to avoid applying to the wrong job.
"""
import base64
import re
from difflib import SequenceMatcher
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
MIN_SIM = 0.85
SUFFIX = r"\b(inc|llc|ltd|limited|gmbh|ab|ag|sa|bv|plc|corp|corporation|co|group|labs|technologies|technology|holdings|the)\b"
AGGREGATORS = ("linkedin.", "indeed.", "glassdoor.", "ziprecruiter.", "jooble.", "simplyhired.", "monster.", "talent.com",
               "learn4good.", "jobrapido.", "adzuna.", "whatjobs.", "careerjet.", "jobleads.", "wellfound.", "builtin.", "facebook.", "youtube.")


ATS_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "recruitee.com", "teamtailor.com", "smartrecruiters.com",
             "personio.de", "personio.com", "bamboohr.com", "breezy.hr", "join.com", "myworkdayjobs.com", "pinpointhq.com",
             "homerun.co", "comeet.com", "rippling.com", "jobs.gusto.com", "applytojob.com")


def _norm(t: str) -> str:
    t = re.sub(r"\((m|f|w|d|x|/|\s)+\)", " ", t.lower())
    t = re.sub(r"ref\s*#?\s*\d+", " ", t)
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _slugs(company: str) -> list[str]:
    base = re.sub(r"[^a-z0-9 ]", " ", company.lower())
    core = re.sub(SUFFIX, " ", base).split()
    out = []
    for words in (core, base.split()):
        if words:
            for s in ("".join(words), "-".join(words)):
                if s not in out:
                    out.append(s)
    return out[:4]


def _json(url: str):
    try:
        r = httpx.get(url, headers=UA, timeout=15, follow_redirects=True)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _ats_postings(slug: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if (d := _json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")):
        out += [(j["title"], j.get("applyUrl") or j["jobUrl"]) for j in d.get("jobs", [])]
    if (d := _json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")):
        out += [(j["title"], j["absolute_url"]) for j in d.get("jobs", [])]
    if (d := _json(f"https://api.lever.co/v0/postings/{slug}?mode=json")) and isinstance(d, list):
        out += [(j["text"], j["hostedUrl"]) for j in d]
    if (d := _json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")):
        out += [(j["title"], j.get("url") or j.get("shortlink", "")) for j in d.get("jobs", [])]
    if (d := _json(f"https://{slug}.recruitee.com/api/offers")):
        out += [(j["title"], j["careers_url"]) for j in d.get("offers", [])]
    if (d := _json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings")):
        out += [(j["name"], f"https://jobs.smartrecruiters.com/{slug}/{j['id']}") for j in d.get("content", [])]
    return [(t, u) for t, u in out if u]


def _bing_target(href: str) -> str:
    """Bing wraps results as bing.com/ck/a?...&u=a1<base64 of the real url>."""
    u = parse_qs(urlparse(href).query).get("u", [""])[0]
    try:
        return base64.urlsafe_b64decode(u[2:] + "=" * (-len(u[2:]) % 4)).decode() if u.startswith("a1") else href
    except Exception:
        return href


def _search(query: str) -> list[str]:
    """Results from DuckDuckGo and Bing (either may rate-limit us), de-duplicated, order kept."""
    urls: list[str] = []
    try:
        r = httpx.get("https://html.duckduckgo.com/html/", params={"q": query}, headers=UA, timeout=20)
        for a in BeautifulSoup(r.text, "html.parser").select("a.result__a"):
            href = a.get("href", "")
            q = parse_qs(urlparse(href).query).get("uddg")
            urls.append(unquote(q[0]) if q else href)
    except Exception:
        pass
    try:
        r = httpx.get("https://www.bing.com/search", params={"q": query, "setlang": "en"}, headers=UA, timeout=20, follow_redirects=True)
        urls += [_bing_target(a.get("href", "")) for a in BeautifulSoup(r.text, "html.parser").select("li.b_algo h2 a")]
    except Exception:
        pass
    seen, out = set(), []
    for u in urls:
        if u.startswith("http") and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _hub_links(url: str) -> list[tuple[str, str]]:
    """Render a careers/listings page and return (anchor text, absolute href) pairs."""
    try:
        from playwright.sync_api import sync_playwright
        from urllib.parse import urljoin
        from .config import env
        with sync_playwright() as pw:
            b = pw.chromium.launch(channel=env("BROWSER_CHANNEL", "msedge"), headless=True)
            p = b.new_page()
            p.set_default_timeout(10000)
            p.goto(url, wait_until="domcontentloaded")
            p.wait_for_timeout(3000)
            pairs = p.evaluate("[...document.querySelectorAll('a[href]')].map(a => [a.innerText || '', a.href])")
            b.close()
            return [(t, urljoin(url, h)) for t, h in pairs if t.strip()]
    except Exception:
        return []


def _registrable(host: str) -> str:
    parts = [p for p in host.split(".") if p]
    if len(parts) < 2:  # 'localhost', empty or malformed website entries
        return host
    return ".".join(parts[-3:] if parts[-2] in ("co", "com", "org") and len(parts) > 2 else parts[-2:])


def _click_through(hub: str, title: str) -> str | None:
    """Listing cards are often JS-clickable boxes, not <a> links. Click the card whose heading is the title; return the new URL."""
    try:
        from playwright.sync_api import sync_playwright
        from .config import env
        with sync_playwright() as pw:
            b = pw.chromium.launch(channel=env("BROWSER_CHANNEL", "msedge"), headless=True)
            p = b.new_page()
            p.set_default_timeout(8000)
            p.goto(hub, wait_until="domcontentloaded")
            p.wait_for_timeout(3000)
            cards = p.get_by_text(re.compile(rf"^\s*{re.escape(title)}\s*$", re.I))
            for i in range(min(cards.count(), 3)):
                cards.nth(i).click()
                p.wait_for_timeout(2500)
                if p.url.rstrip("/") != hub.rstrip("/"):
                    url = p.url
                    b.close()
                    return url
                p.goto(hub, wait_until="domcontentloaded")
                p.wait_for_timeout(2000)
            b.close()
    except Exception:
        pass
    return None


LISTING_RE = re.compile(r"open.?roles|open.?positions|all.?(roles|jobs|positions)|careers?|vacanc|jobs?\b|positions|join us|stellen|lediga", re.I)


def _from_hub(hub: str, title: str, tokens: list[str], depth: int = 0) -> str | None:
    """On a company careers page, follow the link whose text contains the job title, then verify that page.
    If the page is only a landing page, follow its 'open roles / careers / jobs' links one level deeper."""
    want = _norm(title)
    links = _hub_links(hub)
    for text, href in links:
        if len(want) >= 8 and want in _norm(text) and href.rstrip("/") != hub.rstrip("/"):
            if _page_matches(href, title, tokens):
                return href
    if (clicked := _click_through(hub, title)) and _page_matches(clicked, title, tokens):
        return clicked
    if depth == 0:
        host = urlparse(hub).netloc
        seen = {hub.rstrip("/")}
        for text, href in links:
            h = href.split("#")[0]
            if urlparse(h).netloc == host and h.rstrip("/") not in seen and (LISTING_RE.search(text) or LISTING_RE.search(urlparse(h).path)):
                seen.add(h.rstrip("/"))
                if len(seen) > 4:  # cap the crawl
                    break
                if (found := _from_hub(h, title, tokens, depth=1)):
                    return found
    return None


def _rendered(url: str) -> tuple[list[str], str]:
    """Load the page in a real browser (for JavaScript-built career sites) and return (headings, visible text)."""
    try:
        from playwright.sync_api import sync_playwright
        from .config import env
        with sync_playwright() as pw:
            b = pw.chromium.launch(channel=env("BROWSER_CHANNEL", "msedge"), headless=True)
            p = b.new_page()
            p.set_default_timeout(10000)
            p.goto(url, wait_until="domcontentloaded")
            p.wait_for_timeout(2500)
            heads = [h.inner_text() for h in p.locator("h1").all()][:3] + [p.title()]
            text = p.inner_text("body")[:20000]
            b.close()
            return heads, text
    except Exception:
        return [], ""


def _page_matches(url: str, title: str, tokens: list[str]) -> bool:
    """Heading must match the job title AND the page must mention the company (guards against recruiter/client mix-ups).
    If the plain HTML has almost no text (JavaScript-built site), render it in a browser and check that instead."""
    try:
        r = httpx.get(url, headers=UA, timeout=20, follow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")
        heads = [h.get_text(" ", strip=True) for h in soup.find_all(["h1", "title"])]
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            heads.append(og["content"])
        text = soup.get_text(" ")
        if len(re.sub(r"\s+", " ", text).strip()) < 400:  # JS-rendered shell
            heads, text = _rendered(url)
        if not any(_sim(title, h) >= MIN_SIM or (len(_norm(title)) > 12 and _norm(title) in _norm(h)) for h in heads):
            return False
        flat = re.sub(r"[^a-z0-9]", "", (url + " " + text).lower())
        return any(t in flat for t in tokens)
    except Exception:
        return False


def _company_site(li_url: str) -> str | None:
    """The company's own website, read from its public LinkedIn company page (job page -> company link -> JSON-LD)."""
    m = re.search(r"(\d{6,})(?:\?|$)", li_url.split("?")[0] + "?")
    if not m:
        return None
    try:
        jp = httpx.get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{m.group(1)}", headers=UA, timeout=20)
        a = BeautifulSoup(jp.text, "html.parser").select_one("a.topcard__org-name-link")
        if not a:
            return None
        cp = httpx.get(a["href"].split("?")[0], headers=UA, timeout=20, follow_redirects=True)
        for mm in re.finditer(r'(?i)"(?:url|sameAs|website)"\s*:\s*"(https?://[^"]+)"', cp.text):
            if "linkedin" not in mm.group(1):
                return urlparse(mm.group(1)).netloc.lower().removeprefix("www.")
    except Exception:
        pass
    return None


def resolve(job: dict) -> tuple[str | None, str]:
    """Returns (company_site_url or None, how)."""
    company, title = job["company"], job["title"]
    for slug in _slugs(company):
        best = max(((_sim(title, t), u) for t, u in _ats_postings(slug)), default=(0, ""))
        if best[0] >= MIN_SIM:
            return best[1], f"ats:{slug}"
    tokens = [t for s in _slugs(company) for t in [s.replace("-", "")] if len(t) >= 4]
    own_domains: list[str] = []
    site = _company_site(job["url"]) if "linkedin.com" in job["url"] else None
    if site:  # most reliable: the company's real domain, straight from LinkedIn
        own_domains.append(_registrable(site))
        tokens.append(re.sub(r"[^a-z0-9]", "", _registrable(site).split(".")[0]))
        for hub in dict.fromkeys([f"https://talent.{own_domains[0]}", f"https://careers.{own_domains[0]}", f"https://jobs.{own_domains[0]}",
                                  f"https://{own_domains[0]}/careers", f"https://{own_domains[0]}/jobs", f"https://www.{own_domains[0]}/careers", f"https://{site}"]):
            try:
                if httpx.get(hub, headers=UA, timeout=10, follow_redirects=True).status_code != 200:
                    continue
            except Exception:
                continue
            if (found := _from_hub(hub, title, tokens)):
                return found, "careers-hub"
    queries =[f'"{company}" "{title}" apply careers', f'{company} careers {title}', f'{company} open roles jobs']
    for q in queries:
        for u in _search(q)[:10]:
            host = urlparse(u).netloc.lower()
            if any(a in host for a in AGGREGATORS):
                continue
            own = any(t in host.replace("-", "").replace(".", "") for t in tokens)
            # Only the company's own domain or a recognised hiring platform; never random third-party boards.
            if not (own or host.endswith(ATS_HOSTS)):
                continue
            if _page_matches(u, title, tokens):
                return u, "search"
            if own and _registrable(host) not in own_domains:
                own_domains.append(_registrable(host))
    # Found the company's domain but not the exact posting: look inside its likely careers hubs.
    for d in own_domains[:2]:
        for hub in (f"https://talent.{d}", f"https://careers.{d}", f"https://jobs.{d}", f"https://{d}/careers", f"https://{d}/jobs", f"https://www.{d}/careers"):
            try:
                if httpx.get(hub, headers=UA, timeout=10, follow_redirects=True).status_code != 200:
                    continue
            except Exception:
                continue
            if (found := _from_hub(hub, title, tokens)):
                return found, "careers-hub"
    # Last resort (opt-in): the job's own Apply arrow, read from YOUR logged-in LinkedIn session. Free methods came first
    # because clicking Apply on LinkedIn shares your profile with the job poster.
    if "linkedin.com" in job["url"]:
        from .lilogin import external_apply_url
        url, why = external_apply_url(job["url"])
        if url:
            return url, "linkedin-apply-arrow"
        if "Easy Apply" in why:
            return None, "easy-apply"
        if re.search(r"daily cap|paused until|TargetClosed|launch_persistent_context|profile.*in use|Timeout", why):  # a limit, not a real miss: try again later
            return None, f"deferred: {why}"
        return None, f"not found ({why})"
    return None, "not found"
