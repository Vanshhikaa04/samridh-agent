import argparse

from rich.console import Console
from rich.table import Table

from . import db, filters, rag, scoring, sources
from .config import env

con = Console()


def discover():
    c = db.conn()
    from . import scope
    for name, fn in sources.ALL.items():
        if scope.only() and name not in scope.only():
            continue
        try:
            jobs = fn()
        except Exception as e:  # one dead source shouldn't stop the run
            con.print(f"[red]{name} failed:[/] {e}")
            continue
        new = kept = 0
        for j in jobs:
            if not db.upsert(c, j):
                continue
            new += 1
            ok, why = filters.check(j)
            c.execute("UPDATE jobs SET status=?, reason=? WHERE id=?", ("new" if ok else "filtered", why, j["id"]))
            kept += ok
        c.commit()
        con.print(f"{name}: {len(jobs)} fetched, {new} new, {kept} passed filters")


def score_one(c, j: dict) -> str:
    """Score one 'new' job and mark it ready/skipped. Returns the new status."""
    r = scoring.score(j)
    status = "ready" if (j["id"].startswith("yc:open:") or j.get("source") == "yc" or filters.is_internship(j["title"]) or (r["score"] >= int(env("MIN_MATCH_SCORE", "65")) and r.get("startup", True))) else "skipped"
    c.execute("UPDATE jobs SET score=?, reason=?, status=? WHERE id=?", (r["score"], r["reason"], status, j["id"]))
    c.commit()
    con.print(f"{r['score']:>3} {status:<8} {j['title']} @ {j['company']}")
    return status


def score_new():
    c = db.conn()
    for j in c.execute("SELECT * FROM jobs WHERE status='new'").fetchall():
        score_one(c, dict(j))


def show():
    t = Table("score", "title", "company", "location", "url")
    for j in db.conn().execute("SELECT * FROM jobs WHERE status='ready' ORDER BY score DESC"):
        t.add_row(str(j["score"]), j["title"], j["company"], j["location"], j["url"])
    con.print(t)


def main():
    p = argparse.ArgumentParser(prog="jobagent")
    p.add_argument("cmd", choices=["ingest", "discover", "score", "list", "run", "serve", "apply", "apply-url", "daemon", "hunt", "linkedin-login", "assist", "outreach", "outreach-add", "outreach-send", "outreach-replies", "sponsors", "inbox", "india", "site-login", "reset-caps", "yc-outreach", "accel-outreach"])
    p.add_argument("urls", nargs="*", help="job links, for apply-url")
    a = p.parse_args()
    if a.cmd == "accel-outreach":                 # founders of AI companies in accelerator portfolios (Seedcamp, Techstars, Antler ...)
        from . import accel_outreach
        con.print(accel_outreach.run(int(a.urls[0]) if a.urls else 5))
    elif a.cmd == "yc-outreach":                    # email the founders of YC companies for jobs the agent could not apply to by form
        from . import yc_outreach
        con.print(yc_outreach.run(int(a.urls[0]) if a.urls else 8))
    elif a.cmd == "reset-caps":                     # daily caps (applications, mails, LinkedIn lookups, Easy Apply) start again from 0
        from . import caps
        con.print(f"caps reset at {caps.reset()} UTC")
    elif a.cmd == "site-login":                     # python -m jobagent site-login internshala|unstop
        from .sitelogin import login
        login(a.urls[0] if a.urls else "")
    elif a.cmd == "india":                          # India internships with a stated stipend >= INDIA_MIN_STIPEND (default 30000 INR/month)
        from .india import hunt_india
        con.print(hunt_india())
    elif a.cmd == "inbox":                          # track rejections / interviews / replies in your Gmail inbox
        from . import inbox
        con.print(inbox.scan(int(a.urls[0]) if a.urls else 45))
    elif a.cmd == "sponsors":                       # load the Dutch visa-sponsor list and check a batch of their job boards
        from . import sponsors
        c = db.conn()
        if not c.execute("SELECT 1 FROM sponsors LIMIT 1").fetchone():
            con.print(f"synced {sponsors.sync()} sponsor companies")
        con.print(sponsors.discover(int(a.urls[0]) if a.urls else 250))
    elif a.cmd.startswith("outreach"):
        from . import outreach
        c = db.conn()
        if a.cmd == "outreach":                   # find people + write drafts (sends only if OUTREACH_AUTO_SEND=1)
            con.print(outreach.run_cycle())
        elif a.cmd == "outreach-add":             # python -m jobagent outreach-add "Company" company.com
            con.print(f"new drafts: {outreach.add_company(c, a.urls[0], a.urls[1])}")
        elif a.cmd == "outreach-send":            # send everything drafted (respecting the daily cap)
            con.print(f"sent: {outreach.send_queue(c)}")
        else:                                     # mark replies / bounces from your Gmail
            con.print(outreach.check_replies(c))
    elif a.cmd == "assist":  # python -m jobagent assist "Titan OS"  -> visible browser, you solve the CAPTCHA, agent does the rest
        import os
        os.environ["ASSIST"] = "1"
        from .daemon import apply_one
        c = db.conn()
        for name in a.urls:
            c.execute("UPDATE jobs SET status='ready' WHERE company LIKE ? AND status IN ('needs_input','dry_run_ok')", (name + "%",))
            c.commit()
            for r in c.execute("SELECT id, company FROM jobs WHERE company LIKE ? AND status='ready'", (name + "%",)).fetchall():
                con.print(f"{r['company']}: {apply_one(r['id'], live=True)}")
    elif a.cmd == "linkedin-login":
        from .lilogin import login
        login()
    elif a.cmd == "hunt":
        from .hunt import hunt
        con.print(f"hunt: {hunt()}")
    elif a.cmd == "apply-url":
        from .daemon import apply_one
        from .manual import add_link
        live = env("MANUAL_LINK_SUBMIT", "1") == "1"
        for u in a.urls:
            con.print(f"{u} -> {apply_one(add_link(u), live)}")
    elif a.cmd == "daemon":
        from .daemon import run_forever
        run_forever()
    elif a.cmd == "apply":
        from .daemon import apply_ready
        apply_ready()
    elif a.cmd == "serve":
        from .web import serve
        serve()
    elif a.cmd == "ingest":
        con.print(f"indexed {rag.ingest()} chunks")
    elif a.cmd == "discover":
        discover()
    elif a.cmd == "score":
        score_new()
    elif a.cmd == "list":
        show()
    else:
        discover(); score_new(); show()


if __name__ == "__main__":
    main()
