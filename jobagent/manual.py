"""Apply to any job link you give: python -m jobagent apply-url <url> ... (or the dashboard's link box)."""
import hashlib
import re
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from . import db

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}


def add_link(url: str) -> str:
    """Create (or find) a job row for this link, approved by you (status 'ready'). Returns the job id."""
    url = url.strip()
    jid = "manual:" + hashlib.sha1(url.encode()).hexdigest()[:12]
    c = db.conn()
    if c.execute("SELECT 1 FROM jobs WHERE id=?", (jid,)).fetchone():
        return jid
    title, company, desc = "", "", ""
    try:
        html = httpx.get(url, headers=UA, timeout=20, follow_redirects=True).text
        soup = BeautifulSoup(html, "html.parser")
        h1 = soup.find("h1")
        title = (h1.get_text(" ", strip=True) if h1 else (soup.title.get_text(strip=True) if soup.title else ""))[:200]
        og = soup.find("meta", property="og:site_name")
        company = og["content"] if og and og.get("content") else ""
        desc = re.sub(r"\s+", " ", soup.get_text(" "))[:6000]
    except Exception:
        pass
    host = urlparse(url).netloc.removeprefix("www.")
    c.execute(
        "INSERT INTO jobs(id,source,title,company,location,url,description,status,allow_senior,notes) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (jid, "manual", title or url, company or host, "", url, desc, "ready", 1, "added by link"),
    )
    c.commit()
    return jid
