"""RAG over resume + profile + any extra notes in data/knowledge/*.md|txt."""
import json
import re
from pathlib import Path

import chromadb
import yaml
from pypdf import PdfReader

from .config import DATA, ROOT, profile
from .llm import chat, embed

KNOWLEDGE = DATA / "knowledge"
KNOWLEDGE.mkdir(exist_ok=True)


def _col():
    return chromadb.PersistentClient(path=str(DATA / "chroma")).get_or_create_collection("candidate")


def _chunks(text: str, size: int = 700, overlap: int = 100) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size - overlap) if text[i:i + size].strip()]


def resume_text() -> str:
    out = []
    files = sorted(f for f in DATA.glob("*[Rr]esume*") if f.is_file()) + [p for p in [DATA / "Student_Resume.pdf"] if p.exists()]
    for p in files:
        if p.suffix.lower() == ".pdf":
            out.append(f"[{p.name}]\n" + "\n".join(pg.extract_text() or "" for pg in PdfReader(p).pages))
        else:
            out.append(p.read_text(encoding="utf-8"))
    return "\n\n".join(out)


def ingest() -> int:
    docs = {"resume": resume_text(), "profile": yaml.safe_dump(profile(), allow_unicode=True)}
    for f in KNOWLEDGE.glob("*"):
        if f.suffix in {".md", ".txt"}:
            docs[f.name] = f.read_text(encoding="utf-8")
    col = _col()
    ids, chunks, metas = [], [], []
    for name, text in docs.items():
        for i, c in enumerate(_chunks(text)):
            ids.append(f"{name}:{i}"); chunks.append(c); metas.append({"src": name})
    if chunks:
        col.upsert(ids=ids, documents=chunks, metadatas=metas, embeddings=embed(chunks))
    return len(chunks)


INDIA_JOB = re.compile(r"\b(india|bengaluru|bangalore|mumbai|delhi|hyderabad|pune|chennai|gurgaon|gurugram|noida|kolkata|ahmedabad|kochi|jaipur|indore)\b", re.I)
AUTH_KEYS = ["authorized to work", "authorised to work", "right to work", "work authorization", "work authorisation", "eligible to work", "legally"]


def _rule_answer(question: str, job: dict | None = None) -> str | None:
    q = question.lower()
    if job and INDIA_JOB.search(job.get("location") or ""):  # a job in India: you are an Indian citizen, no visa or sponsorship involved
        if any(k in q for k in AUTH_KEYS):
            return "Yes"
        if any(k in q for k in ("sponsor", "visa")):
            return "No"
    for r in profile().get("rules", []):
        if any(k in q for k in r["match"]):
            return r["answer"]
    return None


def retrieve(query: str, k: int = 5) -> str:
    res = _col().query(query_embeddings=embed([query]), n_results=k)
    return "\n---\n".join(res["documents"][0])


def answer(question: str, job: dict, options: list[str] | None = None) -> tuple[str, str]:
    """Returns (answer, via). via = rule | rag | NEEDS_USER. Never invents facts."""
    if (a := _rule_answer(question, job)) is not None:
        return a, "rule"
    ctx = retrieve(question + " " + job["title"])
    if not options and re.search(r"why\b|motivat|what (excites|interests|attracts)|tell us|about yourself|interest(ed)? in", question, re.I):
        # Open-ended "why us?" question: write it from the candidate's real background + what the job posting says.
        sys = ("Write a short, honest answer (3-4 sentences, first person) for the candidate in CONTEXT to this application question. "
               "Connect 1-2 real things from the candidate's background to what the JOB posting says the company does. "
               "Do NOT invent experience, employers, numbers or skills that are not in CONTEXT. No flattery or clichés.")
        user = f"CONTEXT:\n{ctx}\n\nJOB: {job['title']} at {job['company']}\n{job['description'][:2500]}\n\nQUESTION: {question}"
        return chat(sys, user).strip(), "rag"
    sys = ("You fill job applications for the candidate described in CONTEXT. Use ONLY facts in CONTEXT. "
           "If the facts needed are missing, reply exactly NEEDS_USER. Write in first person, concise, "
           "specific to the company and role, no clichés. If OPTIONS are given, reply with exactly one option.")
    user = (f"CONTEXT:\n{ctx}\n\nJOB: {job['title']} at {job['company']}\n{job['description'][:2500]}\n\n"
            f"QUESTION: {question}\nOPTIONS: {options or 'free text'}")
    out = chat(sys, user).strip()
    return (out, "NEEDS_USER") if out == "NEEDS_USER" else (out, "rag")
