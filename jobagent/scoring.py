import json

from .config import search_cfg
from .decide import classify_job
from .filters import region_bonus
from .llm import chat
from .rag import resume_text

SYS = """You rate how well a candidate fits a job. Be strict; most jobs deserve 40-75.
Anchors: 90+ = nearly every requirement is evidenced in the resume; 70 = core skills match, a few gaps;
50 = adjacent field; below 40 = wrong field.
List every MUST-HAVE requirement in the posting (required language such as Swedish/German, specific domain
experience, required tools, required degree) that the resume does NOT evidence.
Judge the employer. "company_size" is "small" (under ~250 employees, e.g. a startup or scale-up), "medium", "large"
(over ~1000 employees, or a well-known established firm such as a bank, asset manager, insurer, telecom, consultancy,
big-tech or large public company), or "unknown". If you recognise the company, use what you know about it.
"startup" is true only for an early/growth-stage startup or small company.
"min_years_required" is the minimum years of professional experience the posting asks for, as a number, or null.
Reply JSON: {"score": 0-100, "reason": "one sentence", "missing_must_haves": ["..."], "startup": true|false,
"company_size": "small|medium|large|unknown", "min_years_required": number|null}"""


def score(job: dict) -> dict:
    # Laya: cheap local yes/no signals on the job itself.
    sig = classify_job(job)
    if sig["ai_role"] < 0.5:
        return {"score": 0, "reason": f"not an AI role (laya {sig['ai_role']:.2f})", "laya": sig}

    # OpenAI: nuanced resume-vs-job fit.
    user = f"RESUME:\n{resume_text()[:5000]}\n\nJOB: {job['title']} at {job['company']} ({job['location']})\n{job['description'][:3500]}"
    r = json.loads(chat(SYS, user, json_mode=True))
    s = int(r.get("score", 0))
    missing = r.get("missing_must_haves") or []
    s = min(s, 85 - 10 * len(missing))  # each unmet must-have costs 10 and caps the score
    if sig["junior_ok"] < 0.3:  # Laya was weak on this in testing, so only a strong 'no' counts
        s = min(s, 40)
    # Small nudges only, applied after the cap logic and never past 95.
    s += round(6 * (sig["startup"] - 0.5)) + (3 if sig["visa"] > 0.6 else 0) + region_bonus(job) // 2
    from .sponsors import is_sponsor
    sponsor = is_sponsor(job["company"])
    if sponsor:  # a confirmed IND-recognised visa sponsor is the single biggest practical plus for you
        s += 10
    r.update(score=max(0, min(95, s)), laya=sig)
    if sponsor:
        r["reason"] += " IND-recognised visa sponsor."

    # Hard rules (not nudges): a medium/large employer is not a startup, and a posting that needs more experience
    # than you have is not a fit, whatever the keyword overlap.
    notes = []
    if r.get("company_size") in ("medium", "large"):
        r["startup"] = False
    if not r.get("startup", True):
        r["score"] = min(r["score"], 40)
        notes.append(f"Not a startup (size: {r.get('company_size', '?')}).")
    yrs = r.get("min_years_required")
    if isinstance(yrs, (int, float)) and yrs > search_cfg()["max_years_required"]:
        r["score"] = min(r["score"], 30)
        notes.append(f"Needs {yrs:g}+ years.")
    r["reason"] += " " + " ".join(notes) if notes else ""
    if missing:
        r["reason"] += " Missing: " + "; ".join(missing)
    return r
