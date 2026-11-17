"""India internships with a stated stipend of at least INDIA_MIN_STIPEND (default 30,000 INR a month).

LinkedIn rarely shows pay, so only postings whose text (or LinkedIn's salary box) actually states a figure at or above the minimum are queued;
the rest are counted but not applied to (pay unknown or too low). Run every cycle from the daemon, or: python -m jobagent india
"""
import logging
import re
import time

from . import db, filters
from .config import env
from .hunt import PAUSE, _patient
from .sources import linkedin_cards, linkedin_detail

log = logging.getLogger("agent")
CITIES = ["India", "Bengaluru, Karnataka, India", "Hyderabad, Telangana, India", "Pune, Maharashtra, India", "Mumbai, Maharashtra, India",
          "Delhi, India", "Gurugram, Haryana, India", "Chennai, Tamil Nadu, India", "Noida, Uttar Pradesh, India"]
KEYWORDS = ["AI Intern", "Machine Learning Intern", "AI Internship", "Data Science Intern", "LLM Intern", "Generative AI Intern",
            "NLP Intern", "AI Research Intern", "Machine Learning Internship", "Deep Learning Intern"]
INDIA = re.compile(r"\b(india|bengaluru|bangalore|mumbai|delhi|hyderabad|pune|chennai|gurgaon|gurugram|noida|kolkata|ahmedabad|kochi|jaipur|indore)\b", re.I)

_NUM = r"(\d[\d,]*(?:\.\d+)?)\s*(k|lakhs?|lacs?|l\b|lpa)?"
_CUR = r"(?:₹|inr|rs\.?|rupees)"


def _val(num: str, unit: str | None) -> float:
    v = float(num.replace(",", ""))
    u = (unit or "").lower()
    if u == "k":
        v *= 1000
    elif u.startswith(("l", "lac", "lak")):
        v *= 100000
    return v


def monthly_inr(text: str) -> int | None:
    """The highest monthly INR figure the text states (annual figures are divided by 12), or None if no pay is stated."""
    t = text.replace("₹", "₹")
    best = None
    pats = [rf"{_CUR}\s*{_NUM}(?:\s*(?:-|–|to)\s*{_CUR}?\s*{_NUM})?", rf"{_NUM}\s*{_CUR}\b", r"stipend[^.\n]{0,50}?" + _NUM, _NUM + r"\s*(?:/|per|a)\s*month"]
    for p in pats:
        for m in re.finditer(p, t, re.I):
            groups = [g for g in m.groups()]
            nums = [(groups[i], groups[i + 1]) for i in range(0, len(groups) - 1, 2) if groups[i]]
            window = t[max(0, m.start() - 15): m.end() + 22].lower()
            annual = bool(re.search(r"per annum|p\.?a\.?\b|annual|lpa|year|yearly|/yr|ctc", window)) and "month" not in window
            for num, unit in nums:
                try:
                    v = _val(num, unit)
                except ValueError:
                    continue
                if v < 3000:   # "2 months", "3 days" etc.
                    continue
                if annual or v >= 1_000_000:
                    v /= 12
                if v > 300_000:   # not a stipend
                    continue
                best = max(best or 0, int(v))
    return best


PAID = re.compile(r"\bpaid\b|stipend|salary|compensation|remuneration|\bpay\b|₹|\binr\b|\brs\.?\s*\d|per month|/month|\blpa\b", re.I)
UNPAID = re.compile(r"\bunpaid\b|no stipend|without (a )?stipend|voluntary|not paid", re.I)


def india_decision(title: str, description: str) -> tuple[str, str, str]:
    """-> (status, reason, notes). Apply when the posting mentions pay (paid / stipend / salary / an amount), whatever the amount; skip unpaid
    postings and postings that never mention pay."""
    text = title + " " + description
    pay = monthly_inr(text)
    if UNPAID.search(text):
        return "skipped", "India internship: unpaid", ""
    if PAID.search(text) or pay is not None:
        amount = f" (~{pay:,} INR/month)" if pay else ""
        return "ready", f"India internship: pay mentioned{amount}", f"India internship: pay mentioned{amount}"
    return "skipped", "India internship: no pay mentioned", ""


def hunt_india(pages: int = 2) -> dict:
    min_pay = int(env("INDIA_MIN_STIPEND", "30000"))
    c = db.conn()
    seen = queued = low = unknown_q = 0
    for city in CITIES:
        for kw in KEYWORDS:
            start = 0
            for _ in range(pages):
                cards = _patient(linkedin_cards, kw, city, start, "1")
                time.sleep(PAUSE)
                if not cards:
                    break
                start += len(cards)
                for card in cards:
                    if c.execute("SELECT 1 FROM jobs WHERE id=?", (card["id"],)).fetchone():
                        continue
                    if not filters.title_ok(card["title"]) or not filters.is_internship(card["title"]):
                        continue
                    detail = _patient(linkedin_detail, card)
                    time.sleep(PAUSE)
                    if not detail:
                        continue
                    seen += 1
                    pay = monthly_inr(detail["title"] + " " + detail["description"])
                    if not db.upsert(c, detail):
                        continue
                    status, reason, notes = india_decision(detail["title"], detail["description"])
                    c.execute("UPDATE jobs SET status=?, score=?, reason=?, notes=? WHERE id=?", (status, 70 if status == "ready" else None, reason, notes, detail["id"]))
                    queued += status == "ready"
                    low += status != "ready"
                    c.commit()
    out = {"seen": seen, "queued_pay_mentioned": queued, "skipped_unpaid_or_no_pay_mentioned": low}
    log.info("india internships: %s", out)
    return out
