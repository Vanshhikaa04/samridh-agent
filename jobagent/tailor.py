"""Tailor the TECHNICAL SKILLS section of the resume to one job. Nothing else on the page changes.

What it may do:   reorder your real skills so the ones the job asks for come first; reword an equivalent term to the job's wording
                  ("RAG Pipelines" -> "Retrieval-Augmented Generation"); add a term ONLY if it already appears in your resume's own
                  text (or in profile.yaml `known_skills`, which is where YOU confirm extra skills you truly have).
What it never does: invent a skill. Anything the job wants that you can't back up is listed as "not added" in the viewer.

The page is edited in place (the four skill rows are erased and rewritten in the same Computer Modern font and size), so the
rest of the PDF stays byte-for-byte what it was. Line counts per row are fixed; a new term replaces the least relevant one.
"""
import json
import re
import shutil
from pathlib import Path

import pymupdf

from . import db
from .config import DATA, profile
from .llm import chat

RES_DIR = DATA / "resumes"
RES_DIR.mkdir(exist_ok=True)
FONT_DIR = DATA / "fonts"
FONT_DIR.mkdir(exist_ok=True)
CATEGORIES = ["AI / LLM", "ML / DL", "Infra & Tools", "Data"]


def _font() -> Path:
    """Computer Modern Roman (what LaTeX resumes use). Copied once from matplotlib's bundled fonts so runtime needs no matplotlib."""
    dst = FONT_DIR / "cmr10.ttf"
    if not dst.exists():
        import matplotlib
        shutil.copy(Path(matplotlib.__file__).parent / "mpl-data" / "fonts" / "ttf" / "cmr10.ttf", dst)
    return dst


def student_resume() -> Path | None:
    p = DATA / "Student_Resume.pdf"
    return p if p.exists() else None


def base_resume(job: dict | None = None) -> Path | None:
    """Your resume; for internship roles, your Student resume (Student_Resume.pdf)."""
    from .filters import is_internship
    if job and is_internship(job.get("title", "")) and student_resume():
        return student_resume()
    return next(iter(sorted(p for p in DATA.glob("*[Rr]esume*.pdf"))), None)


def resume_text(pdf: Path | None = None) -> str:
    d = pymupdf.open(pdf or base_resume())
    return "\n".join(p.get_text() for p in d)


# ------------------------------------------------------------------ reading the skills block
def _bold(font: str) -> bool:
    f = font.upper()
    return "CMB" in f or "BOLD" in f


def _items(text: str) -> list[str]:
    """Split 'A, B (C, D), E' into top-level items, keeping parenthesised groups together."""
    out, depth, cur = [], 0, ""
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return [i for i in out if i]


def read_skills(pdf: Path | None = None) -> dict:
    """Rows of the TECHNICAL SKILLS block with their exact geometry, so they can be rewritten in the same place."""
    doc = pymupdf.open(pdf or base_resume())
    page = doc[0]
    full = resume_text().lower()
    lines = []
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            if l["spans"]:
                lines.append({"text": "".join(s["text"] for s in l["spans"]), "bbox": l["bbox"], "font": l["spans"][0]["font"],
                              "size": l["spans"][0]["size"], "origin": l["spans"][0]["origin"], "color": l["spans"][0]["color"]})
    lines.sort(key=lambda x: (round(x["bbox"][1]), x["bbox"][0]))
    start = next(l for l in lines if l["text"].strip().upper() == "TECHNICAL SKILLS")
    nxt = next(l for l in lines if l["bbox"][1] > start["bbox"][3] and _bold(l["font"]) and l["size"] >= 9.8 and l["bbox"][0] < 40)  # next section heading
    block = [l for l in lines if start["bbox"][3] < l["bbox"][1] < nxt["bbox"][1] - 1]
    labels = [l for l in block if _bold(l["font"]) and l["size"] < 9.8 and l["bbox"][0] < 60]
    rows = []
    for i, lab in enumerate(labels):
        top = lab["bbox"][1] - 1.5
        bot = labels[i + 1]["bbox"][1] - 1.5 if i + 1 < len(labels) else nxt["bbox"][1] - 1
        content = [l for l in block if l["bbox"][0] >= 90 and top <= l["bbox"][1] < bot]
        # re-join LaTeX line-end hyphenation ("Hallucina-" + "tion"): keep the hyphen only if the hyphenated form is in the resume text
        text = ""
        for c in content:
            t = c["text"].rstrip()
            if text.endswith("-"):
                a, b = text.rsplit(" ", 1)[-1][:-1], t.split(" ", 1)[0].strip(",")
                joined = a + b
                text = text[:-1] + t if joined.lower() in full and (a + "-" + b).lower() not in full else text + t
            else:
                text = (text + " " + t).strip()
        rows.append({"label": lab["text"].strip(), "items": _items(text), "lines": len(content),
                     "x0": min(c["bbox"][0] for c in content), "x1": max(c["bbox"][2] for c in content),
                     "bbox": [min(c["bbox"][0] for c in content), min(c["bbox"][1] for c in content),
                              max(c["bbox"][2] for c in content), max(c["bbox"][3] for c in content)],
                     "baselines": [c["origin"][1] for c in content], "size": content[0]["size"], "color": content[0]["color"]})
    right = max(r["x1"] for r in rows)
    for r in rows:
        r["right"] = right
    return {"rows": rows}


# ------------------------------------------------------------------ deciding what to change
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9+#.]", "", s.lower())


def _has(term: str, text: str) -> bool:
    """Whole-word presence. 'Go' must not match 'Google', 'C#' must not match 'ABC#1', 'MVP' must not match 'mvps'."""
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(term.strip().lower())}(?![a-z0-9])", text.lower()))


def plan(job: dict, skills: dict, text: str) -> dict:
    """Ask the model what the job wants, then VERIFY every claim in code: nothing is added unless the resume itself backs it up."""
    known = [str(x) for x in (profile().get("known_skills") or [])]
    have = {r["label"]: r["items"] for r in skills["rows"]}
    sys = ("You tailor the TECHNICAL SKILLS section of a resume to ONE job posting, without inventing anything.\n"
           "Reply JSON: {\"required\": [ONLY concrete technologies, tools, programming languages, frameworks, cloud platforms or technical methods the posting asks for; "
           "no soft skills, no experience descriptions; max 14],\n"
           " \"emphasize\": [existing skill items copied VERBATIM from CURRENT SKILLS that matter most for this job, most relevant first, max 12],\n"
           " \"rename\": [{\"from\": \"existing item verbatim\", \"to\": \"the posting's wording for the very same thing\"}],\n"
           " \"add\": [{\"term\": \"...\", \"category\": \"AI / LLM|ML / DL|Infra & Tools|Data\"}],\n"
           " \"missing\": [required terms the candidate has no evidence for]}\n"
           "Rules: 'rename' only for true synonyms of the same technology (e.g. 'RAG Pipelines' -> 'Retrieval-Augmented Generation (RAG)'), never to claim a different tool. "
           "'add' only for a term that literally appears in RESUME TEXT or CONFIRMED SKILLS. Put everything else the posting asks for in 'missing'.")
    user = (f"JOB: {job['title']} at {job['company']}\n{job['description'][:4500]}\n\nCURRENT SKILLS:\n" +
            "\n".join(f"{k}: {', '.join(v)}" for k, v in have.items()) +
            f"\n\nCONFIRMED SKILLS: {', '.join(known) or '(none)'}\n\nRESUME TEXT:\n{text[:6000]}")
    raw = json.loads(chat(sys, user, json_mode=True))
    existing = {i for v in have.values() for i in v}
    existing_n = {_norm(i): i for i in existing}
    body_txt = re.sub(r"TECHNICAL SKILLS.*?WORK EXPERIENCE", " ", text, flags=re.S | re.I)  # resume text outside the skills block
    known_n = {_norm(k) for k in known}

    emphasize = [existing_n[_norm(e)] for e in raw.get("emphasize", []) if _norm(e) in existing_n][:12]
    job_n = _norm(job["description"] + " " + job["title"])
    generic = {"ai", "ml", "and", "of", "the", "for", "with", "in", "a", "an", "pipelines", "pipeline", "systems", "engineering", "development", "tools"}

    def stems(s: str) -> set[str]:
        return {w[:5] for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in generic and len(w) > 1}

    def real_synonym(frm: str, to: str) -> bool:
        # A reword must (1) share a real word with the original, so 'CI/CD' can never become 'Kubernetes',
        # and (2) use the job posting's own wording, so it is not something the model made up.
        return bool(stems(frm) & stems(to)) and _norm(to) in job_n

    rename = [{"from": existing_n[_norm(r["from"])], "to": r["to"].strip()} for r in raw.get("rename", [])
              if _norm(r.get("from", "")) in existing_n and r.get("to") and len(r["to"]) <= 45 and _norm(r["to"]) != _norm(r["from"])
              and real_synonym(existing_n[_norm(r["from"])], r["to"])][:4]
    add, rejected = [], []
    for a in raw.get("add", []):
        term, cat = (a.get("term") or "").strip(), a.get("category")
        if not term or cat not in CATEGORIES or _norm(term) in existing_n:
            continue
        if _has(term, body_txt) or _norm(term) in known_n:   # the resume itself (or you) vouches for it
            add.append({"term": term, "category": cat})
        else:
            rejected.append(term)
    # "Not added" is decided here from your resume text, not from what the model says: a required term is covered only if it
    # literally appears in your resume (or was reworded from an existing item); everything else is reported to you as missing.
    reworded_to = {_norm(r["to"]) for r in rename}
    required = [t for t in raw.get("required", []) if t]
    missing = [t for t in dict.fromkeys(required + rejected + [m for m in raw.get("missing", []) if m])
               if not _has(t, text) and _norm(t) not in known_n and _norm(t) not in reworded_to]
    return {"required": required, "emphasize": emphasize, "rename": rename, "add": add, "missing": missing}


# ------------------------------------------------------------------ rebuilding the rows
def _wrap(items: list[str], font: pymupdf.Font, size: float, width: float) -> list[str]:
    words = (", ".join(items)).split(" ")
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if font.text_length(trial, fontsize=size) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines


def build(skills: dict, p: dict) -> dict:
    """New item list per row, keeping each row within its original number of lines. Returns rows plus a change report."""
    font = pymupdf.Font(fontfile=str(_font()))
    ren = {r["from"]: r["to"] for r in p["rename"]}
    emph_rank = {e: i for i, e in enumerate(p["emphasize"])}
    adds_by_cat = {}
    for a in p["add"]:
        adds_by_cat.setdefault(a["category"], []).append(a["term"])
    out, dropped = {}, []
    for r in skills["rows"]:
        base = r["items"]
        ordered = sorted(base, key=lambda i: (emph_rank.get(i, 999), base.index(i)))  # relevant first, the rest in their original order
        items = [(ren.get(i, i), i) for i in ordered]
        extra = adds_by_cat.get(r["label"], [])
        pos = sum(1 for _, o in items if o in emph_rank)
        items = items[:pos] + [(t, None) for t in extra] + items[pos:]
        width = r["right"] - r["x0"]
        keep_n = {ren.get(e, e) for e in p["emphasize"]} | set(extra)
        while len(_wrap([n for n, _ in items], font, r["size"], width)) > r["lines"]:
            idx = next((k for k in range(len(items) - 1, -1, -1) if items[k][0] not in keep_n), None)  # least relevant: from the tail
            if idx is None:  # everything is relevant: drop the last relevant one
                idx = len(items) - 1
            dropped.append(items[idx][0])
            items.pop(idx)
        out[r["label"]] = [n for n, _ in items]
    return {"rows": out, "dropped": dropped}


def render(skills: dict, new_rows: dict, dst: Path, src: Path | None = None) -> Path:
    """Erase the four skill rows and write the new text in the same font, size and positions; leave everything else alone."""
    doc = pymupdf.open(src or base_resume())
    page = doc[0]
    fontfile = str(_font())
    for r in skills["rows"]:
        x0, y0, x1, y1 = r["bbox"]
        page.add_redact_annot(pymupdf.Rect(x0 - 0.5, y0 - 0.3, r["right"] + 1, y1 + 0.3), fill=(1, 1, 1))
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
    font = pymupdf.Font(fontfile=fontfile)
    for r in skills["rows"]:
        col = r["color"]
        rgb = (((col >> 16) & 255) / 255, ((col >> 8) & 255) / 255, (col & 255) / 255)
        lines = _wrap(new_rows[r["label"]], font, r["size"], r["right"] - r["x0"])
        for ln, base in zip(lines, r["baselines"]):
            page.insert_text((r["x0"], base), ln, fontname="cmr10x", fontfile=fontfile, fontsize=r["size"], color=rgb)
    dst.parent.mkdir(parents=True, exist_ok=True)
    doc.save(dst, garbage=3, deflate=True)
    return dst


# ------------------------------------------------------------------ public API
def _key(job_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", job_id)[:90]


def tailor(job: dict, force: bool = False) -> dict:
    """Create (or reuse) this job's tailored resume. Returns {'path', 'report'}."""
    c = db.conn()
    row = c.execute("SELECT * FROM resumes WHERE job_id=?", (job["id"],)).fetchone()
    if row and not force and Path(row["path"]).exists():
        return {"path": row["path"], "report": json.loads(row["report"])}
    base = base_resume(job)
    skills, text = read_skills(base), resume_text(base)
    p = plan(job, skills, text)
    built = build(skills, p)
    # what actually changed, per row, for the viewer
    rows = {}
    for r in skills["rows"]:
        before, after = r["items"], built["rows"][r["label"]]
        rows[r["label"]] = {"before": before, "after": after, "added": [x for x in after if x not in before and x not in {z["to"] for z in p["rename"]}],
                            "reworded": [z for z in p["rename"] if z["from"] in before], "removed": [x for x in before if x not in after and x not in {z["from"] for z in p["rename"]}],
                            "reordered": [x for x in after if x in before][:len(after)] != [x for x in before if x in after]}
    report = {"job": f"{job['title']} @ {job['company']}", "required": p["required"], "rows": rows, "not_added": p["missing"], "dropped": built["dropped"]}
    dst = RES_DIR / _key(job["id"]) / base.name   # same file name as your original, so employers see nothing odd
    render(skills, built["rows"], dst, src=base)
    c.execute("INSERT INTO resumes(job_id, path, report, created_at) VALUES(?,?,?,datetime('now')) "
              "ON CONFLICT(job_id) DO UPDATE SET path=excluded.path, report=excluded.report, created_at=excluded.created_at", (job["id"], str(dst), json.dumps(report)))
    c.commit()
    return {"path": str(dst), "report": report}


def resume_for(job: dict) -> Path | None:
    """The file to upload for this job: its tailored resume if tailoring works, otherwise your original (never blocks an application)."""
    try:
        return Path(tailor(job)["path"])
    except Exception:
        return base_resume(job)
