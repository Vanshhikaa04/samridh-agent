"""Every 30 min: discover -> score -> apply to 'ready' jobs. Run: python -m jobagent daemon"""
import logging
import time

from . import apply as applier
from . import db, emailapply
from .config import DATA, env
from .hunt import hunt
from .resolve import resolve

logging.basicConfig(filename=DATA / "agent.log", level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("agent")
INTERVAL = int(env("CYCLE_MINUTES", "30")) * 60
DONE = "('applied','interview','offer','rejected','ghosted')"


def apply_one(job_id: str, live: bool) -> str:
    """Claim the job atomically (ready -> applying) so the daemon, the dashboard and the CLI never double-apply."""
    c = db.conn()
    if c.execute("UPDATE jobs SET status='applying', updated_at=datetime('now') WHERE id=? AND status='ready'", (job_id,)).rowcount == 0:
        c.commit()
        return "not ready"
    c.commit()
    j = dict(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
    origin = ""
    try:
        if j.get("source") in ("yc", "yc_careers") and "ycombinator.com" in j["url"]:  # look up the company's own careers page / board and apply THERE
            from . import yc_careers
            new_url, how = yc_careers.careers_url(j)
            if not new_url:
                return _finish(c, j, applier.Result("needs_input", f"no careers page or hiring board found on the company's website ({how}) - founder email next"), f"YC: {j['url']}")
            c.execute("UPDATE jobs SET url=? WHERE id=?", (new_url, job_id))
            c.commit()
            origin = f"company careers ({how}); YC: {j['url']}"
            j["url"] = new_url
        if j["url"].startswith("mailto:"):
            st, detail = emailapply.apply_by_email(j, j["url"][7:], send=live)
            return _finish(c, j, applier.Result(st, detail), f"posting: {j['id']}")
        from . import sitelogin
        _need = sitelogin.needs_login(j["url"])
        if _need:
            return _finish(c, j, applier.Result("needs_input", f"log in once so the agent can apply on {_need}: python -m jobagent site-login {_need}"), "")
        if "arbetsformedlingen.se/platsbanken" in j["url"]:  # Swedish ad page: the real way to apply is an email or an employer link
            to, link = emailapply.application_target(j)
            if link:
                j["url"] = link
                c.execute("UPDATE jobs SET url=? WHERE id=?", (link, job_id))
                c.commit()
            elif to:
                st, detail = emailapply.apply_by_email(j, to, send=live)
                return _finish(c, j, applier.Result(st, detail), f"ad: {j['url']}")
        if "linkedin.com" in j["url"]:  # no LinkedIn login: find the same job on the company's own site
            li_url = j["url"]
            found, how = resolve(j)
            if not found and how == "easy-apply":  # no company site exists: use LinkedIn's own Easy Apply in your session
                from . import lieasy
                return _finish(c, j, lieasy.apply(j, live), f"LinkedIn Easy Apply: {li_url}")
            if not found and how.startswith("deferred"):
                return _finish(c, j, applier.Result("deferred", f"{how} - will retry automatically"), f"LinkedIn: {li_url}")
            if not found:
                return _finish(c, j, applier.Result("needs_input", f"not found on the company's own site: {how}"), f"LinkedIn: {li_url}")
            origin = f"found on company site via {how}; LinkedIn: {li_url}"
            c.execute("UPDATE jobs SET url=? WHERE id=?", (found, job_id))
            c.commit()
            j["url"] = found
            log.info("%s @ %s -> company site %s (%s)", j["title"], j["company"], found, how)
        from . import tailor
        j["resume_path"] = str(tailor.resume_for(j) or "")  # tailored skills for this exact job; falls back to your original on any error
        r = applier.run(j, submit=live)
    except Exception as e:  # never leave a job stuck in 'applying'
        r = applier.Result("failed", f"{type(e).__name__}: {e}"[:300])
    return _finish(c, j, r, origin)


def _finish(c, j: dict, r, origin: str = "") -> str:
    for q, a, via in r.qa:
        c.execute("INSERT INTO answers(job_id,question,answer,via) VALUES(?,?,?,?)", (j["id"], q, a, via))
    status = {"submitted": "applied", "failed": "needs_input", "drafted": "draft_email", "deferred": "ready"}.get(r.status, r.status)
    note = " | ".join(x for x in (r.detail, origin, f"screenshot: {r.screenshot}" if r.screenshot else "") if x)
    c.execute("UPDATE jobs SET status=?, notes=?, updated_at=datetime('now') WHERE id=?", (status, note, j["id"]))
    if r.status == "submitted":
        c.execute("UPDATE jobs SET applied_at=datetime('now') WHERE id=?", (j["id"],))
    c.commit()
    log.info("%s @ %s -> %s (%s)", j["title"], j["company"], status, r.detail)
    return status


import threading as _th
_APPLY_LOCK = _th.Lock()  # one apply pass at a time (the apply loop and the main cycle both call it)


def apply_ready():
    with _APPLY_LOCK:
        _apply_ready()


def _apply_ready():
    live = env("AUTO_SUBMIT", "0") == "1"
    cap = int(env("MAX_APPLICATIONS_PER_DAY", "8"))
    c = db.conn()
    from . import caps
    done_today = c.execute("SELECT COUNT(*) FROM jobs WHERE date(applied_at)=date('now') AND applied_at > ?", (caps.since(),)).fetchone()[0]
    attempts = 0
    # 'ready' = approved, either by the scorer (>= MIN_MATCH_SCORE, startup) or by you (dropdown / link).
    from .filters import is_internship
    from . import scope
    _src = scope.job_sources()
    _only = (" AND source IN (" + ",".join("'" + x + "'" for x in _src) + ")") if _src else ""
    for j in c.execute("SELECT id, company, title, source FROM jobs WHERE status='ready'" + _only + " AND NOT (COALESCE(notes,'') LIKE 'deferred:%' AND date(updated_at,'localtime')=date('now','localtime')) ORDER BY score DESC").fetchall():
        if live and done_today >= cap:
            log.info("daily cap %d reached", cap)
            break
        if attempts >= APPLY_BATCH:  # one pass = at most this many applications; the next pass starts a few minutes later
            break
        if not search_allowed():  # the window closed (or you paused searching) while this pass was running
            log.info("search window closed: stopping this pass")
            break
        if j["source"] not in ("yc_careers", "accel_careers") and not is_internship(j["title"]) and c.execute(f"SELECT COUNT(*) FROM jobs WHERE company=? AND status IN {DONE}", (j["company"],)).fetchone()[0]:
            c.execute("UPDATE jobs SET status='skipped', notes='already applied to this company' WHERE id=?", (j["id"],))
            c.commit()
            continue
        attempts += 1
        if apply_one(j["id"], live) == "applied":
            done_today += 1


# Failures worth another go on their own: flaky network/browser, rate limits, a missing submit click. Never auto-retried:
# CAPTCHAs (yours), questions only you can answer, and "NOT confirmed" (the application may already have gone through).
TRANSIENT = ("timeout", "err_", "connection", "429", "rate-limiting", "targetclosed", "no submit button", "could not be clicked",
             "net::", "navigation", "browser has been closed", "target page", "page crashed")


def requeue_failed(max_attempts: int = 3) -> int:
    c = db.conn()
    n = 0
    from . import captchasolve
    solver = captchasolve.enabled()  # with a solver key, "CAPTCHA at submit" jobs are retried (the solver handles the challenge)
    for r in c.execute("SELECT id, notes, COALESCE(attempts,0) a FROM jobs WHERE status='needs_input'").fetchall():
        t = (r["notes"] or "").lower()
        blocked = ("cloudflare", "unanswered required", "truthfully", "not confirmed") + (() if solver else ("captcha",))
        if r["a"] >= max_attempts or any(k in t for k in blocked):
            continue
        if any(k in t for k in TRANSIENT) or (solver and "captcha at submit" in t):
            c.execute("UPDATE jobs SET status='ready', attempts=COALESCE(attempts,0)+1, notes='auto-retry: '||substr(COALESCE(notes,''),1,120) WHERE id=?", (r["id"],))
            n += 1
    c.commit()
    if n:
        log.info("auto-retry: %d needs_input job(s) re-queued", n)
    return n


PAUSE_FLAG = DATA / "pause_search.flag"


def search_allowed() -> bool:
    """Searching / applying / contact-finding run only inside SEARCH_WINDOW (e.g. 03:00-07:00, local time), and never while
    data/pause_search.flag exists (the "do not disturb, not even tonight" switch). No SEARCH_WINDOW = always allowed."""
    if PAUSE_FLAG.exists():
        return False
    w = env("SEARCH_WINDOW", "").strip()
    if not w:
        return True
    try:
        a, b = (x.strip() for x in w.split("-"))
        from datetime import datetime
        now = datetime.now().hour * 60 + datetime.now().minute
        start, end = (int(a[:2]) * 60 + int(a[3:5])), (int(b[:2]) * 60 + int(b[3:5]))
        return start <= now < end if start <= end else (now >= start or now < end)
    except Exception:
        return True


def cycle():
    if not search_allowed():
        return
    requeue_failed()
    apply_ready()  # drain what's already queued first; a full hunt can take 30+ minutes
    try:  # a failed hunt must not skip applying and outreach
        result = hunt()
    except Exception:
        log.exception("hunt failed; carrying on with apply + outreach")
        result = "failed"
    log.info("hunt finished: %s", result)
    try:  # India internships that state a stipend of 30,000 INR/month or more
        from . import scope
        if not scope.only():
            from .india import hunt_india
            hunt_india()
    except Exception:
        log.exception("india internship hunt failed")
    apply_ready()


APPLY_EVERY = int(env("APPLY_EVERY_MIN", "10")) * 60  # seconds between apply passes
APPLY_BATCH = int(env("APPLY_BATCH", "10"))  # applications attempted per pass


def _apply_loop():
    """Drain the ready queue every few minutes on its own thread (the hunt can take an hour when LinkedIn rate-limits it)."""
    time.sleep(60)
    last = 0.0
    while True:
        if search_allowed() and time.time() - last >= APPLY_EVERY:
            try:
                requeue_failed()
                apply_ready()
            except Exception:
                log.exception("apply loop failed")
            last = time.time()
        time.sleep(60)


MAIL_EVERY = int(env("MAIL_EVERY_MIN", "10")) * 60  # seconds between mail passes
_MAIL_LOCK = _th.Lock()


def mail_step():
    if env("OUTREACH_ENABLED", "1") != "1":
        return
    with _MAIL_LOCK:
        allowed = search_allowed()
        try:
            from . import outreach
            if allowed:
                log.info("outreach: %s", outreach.run_cycle())
            if outreach.smtp_ready():
                log.info("outreach replies: %s", outreach.check_replies(db.conn()))
            from . import inbox
            log.info("inbox: %s", inbox.scan(14))
        except Exception:
            log.exception("outreach step failed")
        if not allowed:
            return
        try:
            from . import yc_outreach
            yc_outreach.run(int(env("APPLY_BATCH", "10")))
        except Exception:
            log.exception("yc founder outreach failed")
        try:  # other accelerators' portfolios (Seedcamp, Techstars, Antler, Founders Factory, EF, 500 Global)
            from . import accel_outreach
            accel_outreach.run(int(env("ACCEL_PER_PASS", "5")))
        except Exception:
            log.exception("accelerator outreach failed")


def _mail_loop():
    """Outreach + replies + inbox tracking every 20 minutes, independent of how long the hunt takes."""
    time.sleep(90)
    while True:
        mail_step()
        time.sleep(MAIL_EVERY)


def run_forever():
    import threading
    threading.Thread(target=_mail_loop, daemon=True, name="mail-loop").start()
    threading.Thread(target=_apply_loop, daemon=True, name="apply-loop").start()
    mode = "LIVE" if env("AUTO_SUBMIT", "0") == "1" else "DRY-RUN"
    log.info("daemon started, mode=%s, every %ds", mode, INTERVAL)
    print(f"Agent running in {mode} mode, every {INTERVAL // 60} min. Log: data/agent.log")
    last = 0.0
    while True:
        if search_allowed() and time.time() - last >= INTERVAL:
            try:
                cycle()
            except Exception:
                log.exception("cycle failed")  # never let one bad cycle kill the loop
            last = time.time()
        time.sleep(60)
