"""YC company -> the company's OWN careers page / hiring-platform board, so the application goes where the company really takes them.

Order: (1) the company's board on Ashby / Greenhouse / Lever / Workable / Recruitee / SmartRecruiters (public APIs), (2) the careers page linked from the
company's own website. For a specific role we look for that role; for an "open application" we pick the best software / AI role it has open.
Returns (url, how) or (None, reason). The form-filler then does the rest; if the page cannot be filled, the founder email takes over.
"""
import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from . import filters, resolve, yc_outreach

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36", "Accept-Language": "en"}
CAREER_TEXT = re.compile(r"^(careers?|jobs?|join( us| the team)?|we.?re hiring|work (with|at) us|open (roles|positions)|see open roles|view open roles)$", re.I)
CAREER_PATH = re.compile(r"^/(careers?|jobs|join(-us)?|work-with-us|open-roles|company/careers|about/careers|hiring)/?$", re.I)
CAREER_PATHS = ("/careers", "/jobs", "/join", "/join-us", "/company/careers", "/about/careers", "/work-with-us", "/hiring")
SOFTWARE_ROLE = re.compile(r"software|product eng|full.?stack|back.?end|front.?end|founding|forward deployed|member of technical staff|platform|applied|ml\b|machine learning|\bai\b|llm|data eng|agent", re.I)


def _pick_open_role(posts: list[tuple[str, str]]) -> tuple[str, str] | None:
    """For an open application: the best role among the board's postings (AI first, then software), junior-compatible titles only."""
    ok = [(t, u) for t, u in posts if not re.search(r"senior|staff|principal|lead|head|director|manager|vp\b", t, re.I)]
    ai = [(t, u) for t, u in ok if filters.title_ok(t)]
    sw = [(t, u) for t, u in ok if SOFTWARE_ROLE.search(t) and not re.search(r"hardware|mechanical|electrical|firmware|rf |embedded|sales|marketing|recruit", t, re.I)]
    return (ai or sw or [None])[0]


def _careers_hub(domain: str) -> str | None:
    """A careers page reachable from the company's homepage (a link), else a conventional path that exists."""
    for root in (f"https://{domain}", f"https://www.{domain}"):
        try:
            r = httpx.get(root, headers=UA, timeout=20, follow_redirects=True)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        best = None
        for a in soup.select("a[href]"):
            text = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
            href = urljoin(str(r.url), a["href"])
            host = urlparse(href).netloc.lower()
            if not CAREER_TEXT.search(text) and not CAREER_PATH.search(urlparse(href).path):
                continue
            same_site = domain in host or host.endswith(resolve.ATS_HOSTS)
            if same_site and len(text) < 60 and not href.startswith("mailto:"):
                best = best or href.split("#")[0]
        if best:
            return best
        for path in CAREER_PATHS:
            try:
                rr = httpx.get(root.rstrip("/") + path, headers=UA, timeout=15, follow_redirects=True)
                if rr.status_code == 200 and CAREER_PATH.search(urlparse(str(rr.url)).path) and re.search(r"career|job|position|role|hiring", rr.text[:6000], re.I):
                    return str(rr.url)
            except Exception:
                continue
    return None


def careers_url(job: dict) -> tuple[str | None, str]:
    info = yc_outreach.yc_company(job)
    if not info:
        return None, "no YC company page"
    role = re.sub(r"\s*\(open application\)", "", job["title"]).strip()
    open_app = "open application" in job["title"]
    label = info["domain"].split(".")[0]
    # 1) a board on a hiring platform
    for slug in dict.fromkeys(resolve._slugs(info["name"]) + [label]):
        posts = resolve._ats_postings(slug)
        if not posts:
            continue
        if open_app:
            pick = _pick_open_role(posts)
            if pick:
                return pick[1], f"{slug} board: {pick[0][:50]}"
            continue
        best = max(posts, key=lambda p: resolve._sim(role, p[0]))
        if resolve._sim(role, best[0]) >= 0.8:
            return best[1], f"{slug} board: {best[0][:50]}"
    # 2) the company's own careers page
    hub = _careers_hub(info["domain"])
    if hub:
        if not open_app:
            found = resolve._from_hub(hub, role, [label])
            if found:
                return found, "careers page: the role"
        return hub, "careers page"
    return None, "no careers page found"


def careers_for_domain(name: str, domain: str) -> tuple[str | None, str, str]:
    """(url, role title, how) for an open application at a company known only by name + website (accelerator portfolios): its hiring board, else its careers page."""
    label = domain.split(".")[0]
    for slug in dict.fromkeys(resolve._slugs(name) + [label]):
        posts = resolve._ats_postings(slug)
        pick = _pick_open_role(posts) if posts else None
        if pick:
            return pick[1], pick[0][:80], f"{slug} board"
    hub = _careers_hub(domain)
    if hub:
        return hub, "AI Engineer (open application)", "careers page"
    return None, "", "no careers page found"
