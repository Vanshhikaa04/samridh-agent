"""Country-by-country job hunt. Stops the instant `target_ready` jobs are queued as 'ready' to apply.

Order: cheap sources first, then LinkedIn one country at a time (rotating start point so every cycle covers
different countries first). A LinkedIn job only counts toward the target once it is scored 'ready' AND matched to
the company's own posting, because LinkedIn itself can't be applied to without your login.
"""
import json
import logging
import ssl
import time

import httpx
from datetime import datetime, timedelta

from . import db, filters
from .__main__ import discover, score_new, score_one
from .config import DATA, search_cfg
from .resolve import resolve
from .sources import RateLimited, linkedin_cards, linkedin_detail

log = logging.getLogger("agent")
STATE = DATA / "hunt_state.json"
PAUSE = 3.0  # seconds between LinkedIn requests
BACKOFF = (90, 180, 300)  # waits after a 429 before giving up on this cycle


def _patient(fn, *args):
    """Call a LinkedIn fetcher; on 429 wait and retry a few times, then let RateLimited propagate."""
    for wait in BACKOFF:
        try:
            return _net_safe(fn, *args)
        except RateLimited:
            log.info("hunt: 429 from LinkedIn, waiting %ds", wait)
            time.sleep(wait)
    return _net_safe(fn, *args)


def _net_safe(fn, *args):
    """A dropped connection (SSL EOF, reset, timeout) must cost one request, not the whole 30-minute cycle."""
    for attempt in range(3):
        try:
            return fn(*args)
        except RateLimited:
            raise
        except (httpx.HTTPError, OSError, ssl.SSLError) as e:
            log.info("hunt: network error (%s), attempt %d/3", type(e).__name__, attempt + 1)
            time.sleep(5 * (attempt + 1))
    return None


def ready_count(c) -> int:
    return c.execute("SELECT COUNT(*) FROM jobs WHERE status='ready'").fetchone()[0]


def _load_idx() -> int:
    try:
        return int(json.loads(STATE.read_text())["idx"])
    except Exception:
        return 0


def _save_idx(i: int):
    STATE.write_text(json.dumps({"idx": i}))


def _consider(c, job: dict):
    """Store, filter and score one fully-fetched LinkedIn job; queue it only if appliable on the company's site."""
    if not db.upsert(c, job):
        return
    ok, why = filters.check(job)
    if not ok:
        c.execute("UPDATE jobs SET status='filtered', reason=? WHERE id=?", (why, job["id"]))
        c.commit()
        return
    c.execute("UPDATE jobs SET status='new' WHERE id=?", (job["id"],))
    if score_one(c, dict(c.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone())) != "ready":
        return
    url, how = resolve(job)
    if url:
        c.execute("UPDATE jobs SET url=?, notes=? WHERE id=?", (url, f"found on company site via {how}; LinkedIn: {job['url']}", job["id"]))
    else:
        c.execute("UPDATE jobs SET status='needs_input', notes=? WHERE id=?",
                  (f"good match, but not on the company's own site - apply manually: {job['url']}", job["id"]))
    c.commit()


def hunt() -> str:
    cfg = search_cfg()
    target = int(cfg.get("target_ready", 50))
    c = db.conn()

    discover()
    score_new()
    from . import scope as _scope
    try:  # Dutch IND-recognised sponsors: refresh the list monthly, check a batch of their job boards every cycle
        if _scope.only():
            raise StopIteration
        from . import sponsors
        newest = c.execute("SELECT MAX(fetched_at) m FROM sponsors").fetchone()["m"]
        if not newest or newest < (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"):
            log.info("sponsors: synced %d companies from indsponsors.nl", sponsors.sync())
        log.info("sponsors: %s", sponsors.discover(int(search_cfg().get("sponsors", {}).get("batch", 250))))
    except StopIteration:
        pass
    except Exception:
        log.exception("sponsor step failed")
    log.info("hunt: %d/%d ready after non-LinkedIn sources", ready_count(c), target)
    from . import scope
    if scope.only():  # restricted to some sources: no LinkedIn search
        return "restricted to " + ",".join(scope.only())
    if ready_count(c) >= target:
        return "target reached"

    countries = cfg["linkedin_countries"]
    start_idx = _load_idx() % len(countries)
    for step in range(len(countries)):
        i = (start_idx + step) % len(countries)
        country = countries[i]
        _save_idx(i)  # if we stop mid-country (target / rate limit) we resume here next cycle
        found = 0
        try:
            passes = [(kw, True, int(cfg.get("linkedin_pages", 2))) for kw in cfg["linkedin_keywords"]] + \
                     [(kw, False, 1) for kw in cfg.get("linkedin_keywords_open", [])] + \
                     [(kw, "1", 1) for kw in cfg.get("linkedin_keywords_intern", [])]
            for kw, entry_only, pages in passes:
                start = 0
                for _ in range(pages):
                    cards = _patient(linkedin_cards, kw, country, start, entry_only)
                    time.sleep(PAUSE)
                    if not cards:
                        break
                    start += len(cards)
                    for card in cards:
                        if c.execute("SELECT 1 FROM jobs WHERE id=?", (card["id"],)).fetchone():
                            continue
                        ok, why = filters.check(card)  # cheap pre-check on title/company/age/location, no detail fetch
                        if not ok:
                            if db.upsert(c, card):
                                c.execute("UPDATE jobs SET status='filtered', reason=? WHERE id=?", (why, card["id"]))
                                c.commit()
                            continue
                        detail = _patient(linkedin_detail, card)
                        time.sleep(PAUSE)
                        if detail:
                            _consider(c, detail)
                            found += 1
                            if ready_count(c) >= target:
                                log.info("hunt: target %d reached in %s", target, country)
                                return "target reached"
        except RateLimited:
            log.info("hunt: LinkedIn rate-limited in %s; resuming there next cycle (%d/%d ready)", country, ready_count(c), target)
            return "rate limited"
        log.info("hunt: %s done, %d new candidates, %d/%d ready", country, found, ready_count(c), target)
        _save_idx((i + 1) % len(countries))
    return "all countries searched"
