"""Laya: fast typed decisions (yes/no, choice, score) in one forward pass. No text generation.

Used for (a) classifying jobs and (b) picking options on selection-type form questions.
Anything open-ended goes to OpenAI via rag.answer().
"""
from functools import lru_cache

from laya import Router


@lru_cache(maxsize=1)
def _router() -> Router:
    return Router()


def yes_no(text: str, questions: dict[str, str]) -> dict[str, float]:
    """questions: {key: instruction}. Returns {key: P(true)}."""
    qs = {k: {"type": "noul", "instructions": q} for k, q in questions.items()}
    out = _router().predict(text, qs)["answers"]
    return {k: float(out[k]["noul"]) for k in questions}


def choose(text: str, question: str, options: list[str]) -> tuple[str, float]:
    """Pick one option for a dropdown/radio question given context text. Returns (option, confidence)."""
    out = _router().predict(text, {"q": {"type": "choice", "instructions": question, "criteria": options}})["answers"]["q"]
    return out["choice"], float(out["answer_confidence"])


JOB_QUESTIONS = {
    "startup": "Is the employer a startup or small company rather than a large established corporation?",
    "junior_ok": "Is this role suitable for someone with 3 years of experience or less?",
    "ai_role": "Is this a hands-on AI, machine learning, or LLM engineering role?",
    "visa": "Does the posting offer visa sponsorship or relocation support?",
}


def classify_job(job: dict) -> dict[str, float]:
    text = f"{job['title']} at {job['company']} ({job['location']}). {job['description'][:1500]}"
    return yes_no(text, JOB_QUESTIONS)
