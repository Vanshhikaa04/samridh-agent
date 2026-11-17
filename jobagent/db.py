import sqlite3

from .config import DATA

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  source TEXT, title TEXT, company TEXT, location TEXT, url TEXT,
  description TEXT, applicants INTEGER,
  status TEXT DEFAULT 'new',      -- see STATUSES
  score INTEGER, reason TEXT,
  found_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS answers (
  job_id TEXT, question TEXT, answer TEXT, via TEXT,
  at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS companies (
  name TEXT PRIMARY KEY, display TEXT, domain TEXT, role_focus TEXT, contacts_searched_at TEXT
);
CREATE TABLE IF NOT EXISTS resumes (          -- the resume tailored to each job, plus what changed
  job_id TEXT PRIMARY KEY, path TEXT, report TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS sponsors (          -- IND-recognised visa sponsors (indsponsors.nl)
  id TEXT PRIMARY KEY, name TEXT, norm TEXT, kvk TEXT, city TEXT, province TEXT, sector TEXT, size TEXT, website TEXT, domain TEXT,
  description TEXT, linkedin_url TEXT, ai_hint INTEGER DEFAULT 0, fetched_at TEXT, ats_checked_at TEXT, jobs_found INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS outreach (          -- cold emails to CEOs / CTOs / hiring leads
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  company TEXT, display_company TEXT, domain TEXT, job_id TEXT,
  name TEXT, first_name TEXT, title TEXT, email TEXT UNIQUE, source TEXT, confidence TEXT,
  status TEXT DEFAULT 'drafted',               -- drafted | sent | failed | skipped | replied | bounced
  subject TEXT, body TEXT, notes TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP, sent_at TEXT, replied_at TEXT
);
CREATE TABLE IF NOT EXISTS inbox (              -- job-related mail you RECEIVED: rejections, interviews, replies, auto-acks
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id TEXT UNIQUE, from_addr TEXT, from_name TEXT, subject TEXT, received_at TEXT, snippet TEXT,
  kind TEXT,                                   -- rejection | interview | reply | auto_ack
  company TEXT, job_id TEXT, outreach_id INTEGER,
  seen_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

STATUSES = ["new", "filtered", "ready", "applying", "skipped", "needs_input", "draft_email", "dry_run_ok",
            "applied", "interview", "offer", "rejected", "ghosted"]
EXTRA_COLS = ("notes TEXT", "applied_at TEXT", "updated_at TEXT", "allow_senior INTEGER DEFAULT 0", "posted_at TEXT", "attempts INTEGER DEFAULT 0")


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(DATA / "jobs.db", timeout=90)
    c.row_factory = sqlite3.Row
    try:  # several threads/processes (hunt, apply loop, mail loop, dashboard) share this file: WAL lets readers and one writer run together
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=90000")
        c.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.OperationalError:
        pass
    c.executescript(SCHEMA)
    have = {r["name"] for r in c.execute("PRAGMA table_info(jobs)")}
    for col in EXTRA_COLS:
        if col.split()[0] not in have:
            c.execute(f"ALTER TABLE jobs ADD COLUMN {col}")
    have_c = {r["name"] for r in c.execute("PRAGMA table_info(companies)")}
    for col in ("email_pattern TEXT", "hunter_searched_at TEXT", "hr_tried_at TEXT"):
        if col.split()[0] not in have_c:
            c.execute(f"ALTER TABLE companies ADD COLUMN {col}")
    c.commit()
    return c


def upsert(c: sqlite3.Connection, j: dict) -> bool:
    """Insert a job; False if the id exists or the same company+title was already seen (cross-source dedupe)."""
    dup = c.execute("SELECT 1 FROM jobs WHERE lower(company)=lower(?) AND lower(title)=lower(?)",
                    (j["company"], j["title"])).fetchone()
    if dup:
        return False
    cur = c.execute(
        "INSERT OR IGNORE INTO jobs(id,source,title,company,location,url,description,applicants,posted_at)"
        " VALUES(:id,:source,:title,:company,:location,:url,:description,:applicants,:posted_at)",
        {"applicants": None, "posted_at": None, **j},
    )
    c.commit()
    return cur.rowcount > 0
