import re
from datetime import datetime, timezone

from .config import search_cfg


def _years_required(text: str) -> int | None:
    """Smallest 'N+ years' figure mentioned, i.e. the stated minimum."""
    nums = [int(n) for n in re.findall(r"(\d{1,2})\s*\+?\s*(?:-\s*\d+\s*)?(?:years|yrs)", text.lower())]
    return min(nums) if nums else None


AI_TERMS = re.compile(r"\b(ai|a\.i\.|ml|llm|llms|llmops|genai|gen[- ]?ai|generative|machine[- ]learning|artificial intelligence|agent|agents|agentic|"
                      r"nlp|natural language|deep learning|computer vision|mlops|prompt|rag|gpt|neural|data scien\w*|applied scien\w*|research scien\w*)\b", re.I)
ROLE_TERMS = re.compile(r"\b(engineer|engineers|developer|developers|scientist|programmer|researcher|swe|architect|member of technical staff|software)\b", re.I)
BAD_TERMS = re.compile(r"\b(manager|designer|marketing|marketer|sales|growth|seo|trainer|training|producer|recruiter|consultant|consulting|pre-?sales|"
                       r"partnership|partner|account|content|copywriter|writer|support|customer|teacher|instructor|translator|annotator|labeler|labeller|"
                       r"evaluator|tutor|editor|video|community|legal|finance)\b", re.I)
INTERN_BAD = re.compile(r"\b(design|designer|communications?|advocacy|advocate|founder.s office|business|strategy|operations?|people|talent|hr|brand|social media|"
                        r"sales|marketing|finance|legal|accounting|recruit\w*|customer|content|community|event|project management|procurement|supply chain)\b", re.I)
FDE = re.compile(r"forward[- ]deployed", re.I)
INTERN = re.compile(r"\b(interns?|internships?|trainee|werkstudent(in)?|working student|praktikum|praktikant(in)?|stagiaire|co-?op|summer student|placement student)\b", re.I)


def is_internship(title: str) -> bool:
    return bool(INTERN.search(title or ""))


def title_ok(title: str) -> bool:
    """A real AI/ML engineering role: an AI/ML term AND an engineering-type role, and not marketing/design/sales/etc.
    ('LLM Application Engineer', 'AI Systems Engineer', 'ML Platform Engineer' pass; 'AI Growth Manager', 'AI-Native Ad Designer' don't)."""
    if BAD_TERMS.search(title):
        return False
    if INTERN.search(title):  # "AI Intern" has no engineer/developer word: an AI/ML term is enough, but not for non-technical internships
        tech = re.search(r"\b(engineer|engineering|scientist|researcher|machine learning|ml|deep learning|data scien\w*)\b", title, re.I)
        return bool(AI_TERMS.search(title)) and (bool(tech) or not INTERN_BAD.search(title)) and not re.search(r"advocacy|advocate|founder.s office", title, re.I)
    return bool((AI_TERMS.search(title) and ROLE_TERMS.search(title)) or (FDE.search(title) and re.search(r"\bai\b|\bml\b|\bllm\b", title, re.I)))


def is_english(text: str) -> bool:
    """False for French/German/Swedish... postings: you read English, so a local-language ad is not a realistic target."""
    sample = text.strip()[:700]
    if len(sample) < 80:  # too short to judge
        return True
    try:
        from laya import is_english as _en
        return bool(_en(sample))
    except Exception:
        return True


def _startup_ai_role(job: dict) -> bool:
    """YC / Hacker News startups: 'Founding Engineer' or 'Product Engineer' at an AI company has no AI word in the title; the posting says it."""
    if job.get("source") not in ("yc", "hn_jobs", "hn"):
        return False
    t = job["title"]
    from .config import env
    if (job.get("source") == "yc" and env("YC_ANY_ENGINEER", "1") == "1" and ROLE_TERMS.search(t) and not BAD_TERMS.search(t)
            and re.search(r"software|product eng|full.?stack|back.?end|front.?end|founding|forward deployed|member of technical staff|platform|applied|web|mobile|data|llm|agent", t, re.I)
            and not re.search(r"hardware|mechanical|electrical|rf |embedded|firmware|security eng|network|perception|robotics|manufactur|civil|chemical|biolog", t, re.I)):
        return True  # software / product / founding engineer roles at YC companies, AI or not; hardware and other engineering types stay out
    return bool(ROLE_TERMS.search(t) and not BAD_TERMS.search(t) and len(AI_TERMS.findall((job.get("description") or "")[:3500])) >= 4)


def check(job: dict) -> tuple[bool, str]:
    cfg = search_cfg()
    title = job["title"].lower()
    text = (title + " " + job["description"]).lower()
    company = job["company"].lower()

    if not title_ok(job["title"]) and not _startup_ai_role(job):
        return False, "title not AI/ML"
    if is_internship(job["title"]):  # you asked for every AI internship, anywhere: no other rule applies
        return True, "internship"
    if any(x in title for x in cfg["exclude_title"]):
        return False, "seniority in title"
    if any(b in company for b in cfg["big_company_blocklist"]):
        return False, "big company"
    if any(h in company for h in cfg.get("agency_hints", [])):
        return False, "recruitment agency"
    if job.get("description") and not is_english(job["description"]):
        return False, "posting not in English"
    yrs = _years_required(text)
    if yrs is not None and yrs > cfg["max_years_required"]:
        return False, f"requires {yrs}+ years"
    if job.get("applicants") is not None and job["applicants"] >= cfg["max_applicants"]:
        return False, "too many applicants"

    if any(x in job["location"].lower() for x in cfg.get("exclude_locations", [])):
        return False, "located in India"
    if any(p in text for p in cfg.get("no_sponsor_phrases", [])):
        return False, "no visa sponsorship / relocation"
    if (lang := _required_language(text)):
        return False, f"requires {lang}"
    # Startups keep a role open for weeks, and a confirmed visa sponsor is worth a longer look: 30 days instead of 7.
    limit = 30 if job.get("source") in ("sponsor", "hn", "yc", "hn_jobs", "stealth") else cfg.get("max_age_days", 3)
    if (age := _age_days(job.get("posted_at"))) is not None and age > limit:
        return False, f"posted {age:.0f} days ago"
    return True, "ok"


LANGS = {
    "swedish": ["swedish", "svenska"], "finnish": ["finnish", "finska", "suomi"], "german": ["german", "deutsch", "tyska"],
    "french": ["french", "français", "francais"], "dutch": ["dutch", "nederlands"], "danish": ["danish", "dansk"],
    "norwegian": ["norwegian", "norsk"], "polish": ["polish", "polski"], "spanish": ["spanish", "español", "espanol"],
    "italian": ["italian", "italiano"], "portuguese": ["portuguese", "português"], "czech": ["czech"], "estonian": ["estonian", "eesti"],
    "japanese": ["japanese", "日本語", "nihongo"], "korean": ["korean", "한국어"], "chinese": ["chinese", "mandarin", "cantonese", "中文"],
    "hungarian": ["hungarian", "magyar"], "hebrew": ["hebrew", "עברית"], "arabic": ["arabic", "العربية"], "turkish": ["turkish", "türkçe"],
    "russian": ["russian", "русский"], "romanian": ["romanian", "română"], "greek": ["greek", "ελληνικά"], "bulgarian": ["bulgarian"],
    "ukrainian": ["ukrainian"], "slovak": ["slovak"], "lithuanian": ["lithuanian"], "latvian": ["latvian"], "croatian": ["croatian"],
}
NEED = re.compile(r"fluent|fluency|native|proficien|obehindrat|flytande|goda kunskaper|verhandlungssicher|fließend|fliessend|required|mandatory|must|krav|require")
SOFT = re.compile(r"\bplus\b|advantage|nice to have|bonus|beneficial|meriterande|vorteil|desirable|preferred")


def _required_language(text: str) -> str | None:
    """A language you don't list in your profile that the posting makes a requirement (not 'a plus')."""
    from .config import profile
    mine = {x.lower() for x in profile().get("languages", ["english"])}
    for lang, aliases in LANGS.items():
        if lang in mine:
            continue
        for a in aliases:
            for m in re.finditer(rf"\b{a}\b", text):
                window = text[max(0, m.start() - 70): m.end() + 70]
                if NEED.search(window) and not SOFT.search(window):
                    return lang
    return None


def _age_days(posted_at: str | None) -> float | None:
    if not posted_at:
        return None
    try:
        d = datetime.fromisoformat(posted_at)
        return (datetime.now(timezone.utc) - (d if d.tzinfo else d.replace(tzinfo=timezone.utc))).total_seconds() / 86400
    except ValueError:
        return None


def region_bonus(job: dict) -> int:
    """Nudge preferred countries up so DE/FR are kept but don't dominate."""
    cfg = search_cfg()
    loc = job["location"].lower()
    if any(c in loc for c in cfg["preferred_countries"] + ["sweden"]):
        return 8
    if "remote" in loc:
        return 5
    return 0
