"""Generic ATS form filler (Playwright). Works on Teamtailor/Greenhouse/Lever/Ashby-style pages.

Safety contract: submit ONLY if every required field got a confident answer and no CAPTCHA is present.
Otherwise return needs_input and leave the page unsubmitted.
"""
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from playwright.sync_api import Page, sync_playwright
from pypdf import PdfReader

from . import rag
from .config import DATA, env, profile, search_cfg
from .decide import choose

SCREENS = DATA / "screens"
SCREENS.mkdir(exist_ok=True)
MIN_CHOICE_CONF = 0.6
FACT_RE = re.compile(r"fluen|speak|proficien|experience|years|certif|degree|citizen|authori[sz]ed|licen[sc]e|skill|knowledge|"
                     r"familiar|language|clearance|master|bachelor|phd|diploma|resident|based in|currently", re.I)
WILL_RE = re.compile(r"willing|ok with|okay with|comfortable|agree|consent|able to (come|work|travel|commute)|open to|available|accept", re.I)

DEEP_JS = """const deep = (sel, root = document) => {
  const out = [...root.querySelectorAll(sel)];
  for (const el of root.querySelectorAll('*')) if (el.shadowRoot) out.push(...deep(sel, el.shadowRoot));
  return out;
};"""


SCAN_JS = """() => {
  %DEEP%
  const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
  const lab = e => {
    let t = e.getAttribute('aria-label') || '';
    if (!t && e.id) { const l = e.getRootNode().querySelector(`label[for="${CSS.escape(e.id)}"]`); if (l) t = l.innerText; }
    if (!t) { const lb = e.getAttribute('aria-labelledby'); if (lb) t = lb.split(' ').map(i => (e.getRootNode().getElementById ? e.getRootNode().getElementById(i) : document.getElementById(i) || {}).innerText || '').join(' '); }
    if (!t) { const l = e.closest('label'); if (l) t = l.innerText; }
    if (!t) {  // many custom forms put the label as plain text just above the input
      let n = e;
      for (let i = 0; i < 4 && !t && n; i++, n = n.parentElement) {
        const prev = n.previousElementSibling;
        if (prev && !prev.querySelector('input,textarea,select') && prev.innerText && prev.innerText.trim().length > 0 && prev.innerText.trim().length < 90) t = prev.innerText;
      }
    }
    return clean(t || e.placeholder || e.name || '');
  };
  const groupQ = e => {
    const fs = e.closest('fieldset'); const lg = fs && fs.querySelector('legend'); if (lg) return clean(lg.innerText);
    // Climb until we reach real question text (not just "YES"/"NO" option text) or a short text block right above the group.
    let n = e.parentElement;
    for (let i = 0; i < 6 && n; i++, n = n.parentElement) {
      const first = clean((n.innerText || '').split('\\n')[0]);
      if (first.length > 12 && !/^(yes|no)\\b/i.test(first)) return first;
      const prev = n.previousElementSibling;
      if (prev && !prev.querySelector('input,textarea,select')) {
        const t = clean(prev.innerText || '');
        if (t.length > 12 && t.length < 250) return t;
      }
    }
    const p = e.closest('[class*=field],[class*=question],[role=group],[role=radiogroup]') || e.parentElement.parentElement;
    return clean(((p && p.innerText) || '').split('\\n')[0]);
  };
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    if (e.type === 'file') return true;  // usually display:none behind a custom 'Browse' button
    if (e.getAttribute('aria-hidden') === 'true' && !['file','radio','checkbox'].includes(e.type)) return false;  // hidden twins of autocomplete inputs
    return s.display !== 'none' && s.visibility !== 'hidden' && (r.width > 0 || ['file','radio','checkbox'].includes(e.type)); };
  const out = [], groups = {}; let n = 0;
  deep('input,textarea,select').forEach(e => {
    if (['hidden','submit','button','image','reset','search'].includes(e.type) || !vis(e) || (e.readOnly && !['checkbox','radio','file'].includes(e.type))) return;
    e.setAttribute('data-ja', 'f' + (n++));
    // Prefer stable selectors: React pages (Ashby, Greenhouse) re-render and drop our data-ja marker.
    const sel = (() => {
      if (e.id && deep('#' + CSS.escape(e.id)).length === 1) return '#' + CSS.escape(e.id);
      if (e.name && (e.type === 'radio' || e.type === 'checkbox') && e.hasAttribute('value') && e.getAttribute('value')) return `input[name="${CSS.escape(e.name)}"][value="${CSS.escape(e.value)}"]`;
      if (e.name && deep(`[name="${CSS.escape(e.name)}"]`).length === 1) return `[name="${CSS.escape(e.name)}"]`;
      return `[data-ja="${e.getAttribute('data-ja')}"]`;
    })();
    const req = e.required || e.getAttribute('aria-required') === 'true' || /\\*/.test(lab(e));
    if (e.type === 'radio') {
      const k = e.name || groupQ(e);
      if (!groups[k]) { groups[k] = {kind: 'radio', label: groupQ(e), required: req, options: []}; out.push(groups[k]); }
      groups[k].options.push({sel, label: lab(e)}); groups[k].required = groups[k].required || req;
    } else if (e.tagName === 'SELECT') {
      out.push({kind: 'select', sel, label: lab(e) || groupQ(e), required: req,
                options: [...e.options].filter(o => o.value !== '').map(o => ({id: o.value, label: clean(o.text)}))});
    } else if (e.type === 'checkbox') {
      out.push({kind: 'checkbox', sel, label: lab(e) || groupQ(e), group: groupQ(e), required: req});
    } else if (e.type === 'file') {
      out.push({kind: 'file', sel, label: lab(e) || groupQ(e), required: req});
    } else {
      out.push({kind: e.tagName === 'TEXTAREA' ? 'textarea' : 'text', sel, label: lab(e), required: req, type: e.type});
    }
  });
  return out;
}"""


# Ashby and similar forms render Yes/No questions as two <button>s, not radio inputs, so SCAN_JS never sees them.
YESNO_JS = """() => {
  const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
  const isYN = b => /^(yes|no)$/i.test(clean(b.innerText));
  const out = [], seen = new Set();
  document.querySelectorAll('button').forEach(b => {
    const grp = b.parentElement;
    if (!isYN(b) || !grp || seen.has(grp)) return;
    seen.add(grp);
    const btns = [...grp.children].filter(x => x.tagName === 'BUTTON' && isYN(x));
    if (btns.length !== 2) return;
    let q = '', n = grp.parentElement;
    for (let i = 0; i < 5 && n && !q; i++, n = n.parentElement) {
      const first = clean((n.innerText || '').split('\\n')[0]);
      if (first.length > 12 && !/^(yes|no)$/i.test(first)) q = first;
    }
    const id = String(out.length);
    grp.setAttribute('data-ja-yn', id);
    out.push({id, label: q, required: /\\*/.test(q)});
  });
  return out;
}"""


CONFIRM_RE = re.compile(
    r"\b(thank you|thanks for (applying|your)|application (has been )?(received|submitted|sent)|we have received your|we.ll be in touch|"
    r"successfully submitted|"
    r"tack\b|vi har fått din ansökan|din ansökan (har )?(skickats|mottagits|är mottagen)|"            # Swedish ("tack" as a whole word: not "stack")
    r"vielen dank|danke\b|bewerbung (ist |wurde )?(eingegangen|erhalten|gesendet)|"                      # German
    r"merci\b|candidature (a été |bien )?(envoyée|reçue|enregistrée)|nous avons bien reçu|"             # French
    r"gracias por|hemos recibido tu|bedankt|sollicitatie (is )?ontvangen|grazie)\b")                   # Spanish / Dutch / Italian
# Field-level validation wording only; generic words like "required" appear in job descriptions and would block real successes.
ERROR_RE = re.compile(r"this field is required|please (enter|select|choose|fill|provide)|välj minst|vi behöver din|fyll i |"
                      r"can.t be blank|cannot be blank|invalid (email|phone)|ogiltig")


CLOSED_RE = re.compile(r"page you were looking for doesn.t exist|page not found|no longer (available|accepting|open)|"
                       r"position (has been )?(filled|closed)|(job|role|vacancy|posting) (is |has been )?(closed|removed|expired)|"
                       r"inte längre tillgänglig|annonsen är inte längre|this job is no longer|offre (n.est|est) plus|n.est plus disponible", re.I)


def detect_submission(body: str, visible_errors: int = 0, open_question: bool = False) -> tuple[bool, str]:
    """Decide if an application really went through. Needs positive proof AND no unresolved problem.
    Returns (submitted, evidence). Evidence is stored in the tracker notes so every 'applied' is auditable."""
    b = body.lower()
    if open_question:
        return False, "a question pop-up is still open"
    if visible_errors:
        return False, f"{visible_errors} field(s) flagged invalid on the page"
    if (m := ERROR_RE.search(b)):
        return False, f"validation message on page: '{m.group(0)}'"
    if (m := CONFIRM_RE.search(b)):
        return True, f"confirmation text: '{m.group(0)}'"
    return False, "no confirmation text found"


@dataclass
class Result:
    status: str                      # submitted | dry_run_ok | needs_input | failed
    detail: str = ""
    qa: list = field(default_factory=list)   # (question, answer, via)
    screenshot: str = ""


HEAR_RE = re.compile(r"how did you (hear|find)|where did you (hear|see|find)|where you saw us|how you heard|source of (your )?application|connaissance de cette offre|wie sind sie auf uns|var hörde du|hoe heb je|var såg du|hur (fick|hörde) du|first (see|saw|hear|heard|learn)|wo haben sie|wie haben sie|comment avez-vous (connu|entendu)|waar heb je|hoe ben je", re.I)


def _personal(label: str, typ: str = "") -> str | None:
    p = profile()
    if HEAR_RE.search(label):  # true: the agent found the job on LinkedIn
        return "LinkedIn"
    if typ == "email":  # input type is language-independent
        return p["email"]
    if typ == "tel":
        return p["phone"]
    first, _, last = p["name"].partition(" ")
    l = label.lower()
    l = l + " " + re.sub(r"[\s_\-]+", "", l)  # also match "firstName" / "first_name" style labels
    if "relocat" in l:  # "open to relocation?" is a yes/no question, not a request for your city
        return None
    if re.search(r"colleague|recommend|referr|employer|company|school|universit|manager|supervisor|emergency|spouse|friend|family|reference|who told you", l) and "name" in l:
        return None  # e.g. "Name of the colleague who recommended you": leave blank, never your own name
    if "linkedin" in l and ("github" in l or "portfolio" in l):
        return ", ".join(x for x in (p["linkedin"], p["github"]) if x)
    table = [
        (["firstname", "first name", "given name", "förnamn", "fornamn", "vorname", "voornaam", "prénom", "prenom", "nombre"], first),
        (["lastname", "last name", "surname", "family name", "efternamn", "nachname", "achternaam", "apellido"], last or first),
        (["full name", "your name", "namn", "name", "naam", "nom"], p["name"]),
        (["email", "e-mail", "e-post", "epost", "courriel", "correo"], p["email"]),
        (["phone", "mobile", "telefon", "téléphone", "telefoon", "teléfono", "handy"], p["phone"]),
        (["linkedin"], p["linkedin"]),
        (["github"], p["github"]),
        (["portfolio", "website", "personal site"], p.get("portfolio") or p["github"]),
        (["years of experience", "years experience", "how many years", "experience do you have"], str(p["years_experience"])),
        (["salary", "compensation", "lön"], str(p["salary_expectation_eur"])),
        (["notice"], p["notice_period"]),
        (["current location", "city", "where are you based", "currently located", "located", "location", "based"], p["location"]),
    ]
    for keys, val in table:
        if any(k in l for k in keys) and val:
            return val
    return None


def _match_option(answer: str, options: list[dict]) -> dict | None:
    a = answer.lower().strip()
    for o in options:
        if o["label"].lower().strip() == a:
            return o
    first = a.split()[0] if a else ""
    for o in options:
        if first and o["label"].lower().strip().startswith(first):
            return o
    return None


# Visa / work-authorization dropdowns word their options in many ways ("I will require sponsorship", "Yes, I need a visa", ...).
NEEDS_SPONSOR_RE = re.compile(r"\b(require|requires|need|needs|will need|would need)\b.*\b(sponsor|visa|permit)|\bnot (currently |yet )?(authori[sz]ed|eligible|permitted)|"
                              r"\bno\b.*\b(sponsor|visa|work)|sponsorship (is )?(required|needed)|\bwith sponsorship\b|\bvisa required\b", re.I)
NO_SPONSOR_RE = re.compile(r"(do not|don.t|will not|won.t|not|no) (currently )?(require|need)|without (the need for )?(visa )?sponsorship|"
                           r"\b(citizen|permanent resident|green card|settled status|right to work)\b|already (authori[sz]ed|eligible|have)", re.I)


def _sponsorship_option(rule: str, label: str, options: list[dict]) -> dict | None:
    """She needs a visa. When the exact words of a visa/work-status rule match no option, pick the ONE option that says she needs sponsorship."""
    if not (re.search(r"sponsor", rule, re.I) or (rule.strip().lower() == "yes" and re.search(r"sponsor|visa", label, re.I))):
        return None
    hits = [o for o in options if NEEDS_SPONSOR_RE.search(o["label"]) and not NO_SPONSOR_RE.search(o["label"])]
    return hits[0] if len(hits) == 1 else None


def _pick(f: dict, job: dict) -> tuple[dict | None, str, str]:
    """Choose an option for a radio/select. Rules first, then Laya, else None."""
    rule = rag._rule_answer(f["label"], job)
    if rule is not None:
        o = _match_option(rule, f["options"]) or _sponsorship_option(rule, f["label"], f["options"])
        if o:
            return o, rule, "rule"
    labels = [o["label"] for o in f["options"]]
    q = f["label"].lower()
    if re.search(r"how did you (hear|find)|where did you (hear|see|find)|where you saw us|connaissance de cette offre|wie sind sie auf uns|var hörde du|hoe heb je|var såg du|såg du (först )?(den här )?annons|hur (fick|hörde) du|first (see|saw|hear|heard|learn)|wo haben sie|wie haben sie|comment avez-vous (connu|entendu)|d[oó]nde (viste|nos|has)|waar heb je|hoe ben je", q):
        for o in f["options"]:  # true: the agent found the job on LinkedIn
            if re.search(r"linkedin", o["label"], re.I):
                return o, o["label"], "rule"
    if re.search(r"\bgender\b|\bgenre\b|geschlecht|\bsexe\b|\bkön\b", q):  # never guess someone's gender
        for o in f["options"]:
            if re.search(r"prefer not|rather not|ne se prononce|keine angabe|vill inte|decline|undisclosed", o["label"], re.I):
                return o, o["label"], "rule"
    if WILL_RE.search(f["label"]) and not FACT_RE.search(f["label"]):
        # Willingness/agreement ("ok with a background check?"): not a fact about you, Laya may decide.
        ctx = rag.retrieve(f["label"]) + f"\nJob: {job['title']} at {job['company']}"
        name, conf = choose(ctx, f["label"], labels)
        if conf >= MIN_CHOICE_CONF:
            return _match_option(name, f["options"]), name, f"laya({conf:.2f})"
        return None, "", f"laya-unsure({conf:.2f})"
    # Facts about you (languages, experience, degrees, citizenship...): answer ONLY from resume/profile, else park.
    ans, via = rag.answer(f["label"], job, options=labels)
    o = _match_option(ans, f["options"]) if via != "NEEDS_USER" else None
    return (o, ans, "grounded") if o else (None, "", "no-grounded-answer")


def _has_captcha(page: Page) -> bool:
    return page.locator(
        "iframe[src*='recaptcha'], iframe[src*='hcaptcha'], iframe[src*='captcha-delivery'], iframe[src*='challenges.cloudflare'], "
        "iframe[src*='arkoselabs'], iframe[src*='funcaptcha'], .g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey]"
    ).count() > 0


def _resume() -> Path | None:
    return next(iter(sorted(DATA.glob("*[Rr]esume*.pdf"))), None)


def _cover_pdf() -> Path | None:
    return next(iter(sorted(DATA.glob("*[Cc]over*.pdf"))), None)


def _cover_text(job: dict) -> str | None:
    """Your own cover letter, verbatim. {company}/{role} placeholders are filled if you use them."""
    p = _cover_pdf()
    if not p:
        return None
    text = "\n".join(pg.extract_text() or "" for pg in PdfReader(p).pages).strip()
    return text.replace("{company}", job["company"]).replace("{role}", job["title"]) or None


def _title_blocked(page: Page) -> str | None:
    """The posting's own heading is the source of truth; feeds sometimes drop 'Senior'."""
    try:
        t = page.locator("h1").first.inner_text(timeout=2000)
    except Exception:
        return None
    bad = [x for x in search_cfg()["exclude_title"] if re.search(rf"(?<!\w){re.escape(x)}(?!\w)", t, re.I)]
    return f"page heading '{t.strip()}' contains {bad}" if bad else None


CLICK_OPTION_JS = """([q, opt, count]) => {
  %DEEP%
  const norm = s => (s || '').replace(/\\s+/g, ' ').replace(/^\\*\\s*/, '').trim().toLowerCase();
  const want = norm(q).slice(0, 40);
  for (const r of deep('input[type=radio]')) {
    const lab = r.closest('label') || (r.id && r.getRootNode().querySelector(`label[for="${CSS.escape(r.id)}"]`)) || r.parentElement;
    if (!lab || norm(lab.innerText) !== norm(opt)) continue;
    let n = r.parentElement;
    for (let i = 0; i < 7 && n; i++, n = n.parentElement) {
      if (!norm(n.innerText).includes(want)) continue;
      // the nearest container holding the question must hold only THIS question's buttons
      if (n.querySelectorAll('input[type=radio]').length === count) { lab.click(); return r.checked || true; }
      break;
    }
  }
  return false;
}"""


def _click_radio(scope, f: dict, o: dict) -> bool:
    """Choose a radio option. Stable selector first; on pages that swap ids on click, click by question + option text."""
    try:
        scope.locator(o["sel"]).check(force=True, timeout=2500)
        return True
    except Exception:
        pass
    try:
        return bool(scope.evaluate(CLICK_OPTION_JS, [f["label"], o["label"], len(f["options"])]))
    except Exception:
        return False


CLICK_CHECKBOX_JS = """([q]) => {
  %DEEP%
  const norm = s => (s || '').replace(/\\s+/g, ' ').replace(/^\\*\\s*/, '').trim().toLowerCase();
  const want = norm(q).slice(0, 35);
  for (const c of deep('input[type=checkbox]')) {
    const lab = c.closest('label') || (c.id && c.getRootNode().querySelector(`label[for="${CSS.escape(c.id)}"]`)) || c.parentElement;
    const txt = norm((lab && lab.innerText) || c.getAttribute('aria-label') || '') + ' ' + norm((c.parentElement.parentElement || {}).innerText || '');
    if (!txt.includes(want)) continue;
    if (!c.checked) (lab || c).click();
    return c.checked;
  }
  return false;
}"""


ARIA_BOX_JS = """e => { let n = e.parentElement;
  for (let i = 0; i < 4 && n; i++, n = n.parentElement) { const b = n.querySelector('[role=checkbox]'); if (b) return b; }
  return null; }"""


def _check_box(scope, f: dict) -> bool:
    """Tick a checkbox and confirm it is ticked. Modern UI kits (Radix, MUI...) hide the real <input> and draw a button with
    role=checkbox: that button is what the form listens to, so it is clicked first and its aria-checked state is what we verify."""
    if scope.locator(f["sel"]).count() == 0:  # the page re-rendered since the scan (e.g. after the resume upload): find it again by its label
        try:
            for g in scope.evaluate(SCAN_JS):
                if g["kind"] == "checkbox" and g["label"] == f["label"]:
                    f = {**f, "sel": g["sel"]}
                    break
        except Exception:
            pass
    loc = scope.locator(f["sel"]).first

    def aria():
        try:
            return loc.evaluate_handle(ARIA_BOX_JS).as_element()
        except Exception:
            return None

    def ticked() -> bool:
        h = aria()
        if h:
            try:
                return h.get_attribute("aria-checked") == "true"
            except Exception:
                pass
        try:
            return loc.is_checked()
        except Exception:
            return False

    def click_aria():
        h = aria()
        if h:
            try:  # a normal click waits until the box is ready; a forced click can land while the page is still updating
                h.scroll_into_view_if_needed(timeout=2000)
                h.click(timeout=3000)
            except Exception:
                h.click(force=True, timeout=2500)

    # The first click on some pages is swallowed (e.g. it only closes a dropdown from the previous field), so the real button gets two tries.
    # The state is re-read after a pause before each retry, so a click that did work is never undone by a second one.
    for attempt in (click_aria, click_aria, lambda: loc.check(force=True, timeout=2500), lambda: loc.click(force=True, timeout=2500),
                    lambda: scope.evaluate(CLICK_CHECKBOX_JS, [f["label"]])):
        if ticked():
            return True
        try:
            attempt()
        except Exception:
            pass
        try:
            scope.wait_for_timeout(700)
        except Exception:
            pass
    return ticked()


DEEP_TEXT_JS = """() => {
  const txt = root => { let t = root.innerText || root.textContent || '';
    root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) t += ' ' + txt(el.shadowRoot); }); return t; };
  return txt(document.body);
}"""


def _body_text(page) -> str:
    """Visible page text including web components (shadow DOM), where many career sites render the form AND the thank-you message."""
    try:
        return page.evaluate(DEEP_TEXT_JS)
    except Exception:
        return page.inner_text("body")


def _tick(loc):
    """Check a checkbox/radio. Custom-styled ones ignore clicks on the hidden input, so fall back to its label."""
    try:
        loc.check(force=True, timeout=4000)
        return
    except Exception:
        pass
    try:
        loc.locator("xpath=ancestor::label[1]").click(timeout=4000)
    except Exception:
        loc.evaluate("e => (e.closest('label') || e.parentElement).click()")


def _same(a: str, b: str, digits: bool) -> bool:
    digits = digits or bool(re.fullmatch(r"\+?[\d\s().-]{7,}", b.strip()))  # a phone number, whatever the box's type says
    norm = (lambda s: re.sub(r"\D", "", s)) if digits else (lambda s: re.sub(r"\s+", "", s).lower())
    x, y = norm(a), norm(b)
    if digits and len(x) >= 7 and len(y) >= 7:  # a country-code dropdown may already hold +91, leaving only the national number in the box
        return x == y or x.endswith(y) or y.endswith(x)
    return x == y


SALARY_RE = re.compile(r"salary|compensation|expected pay|pay expectation|remuneration|\blön\b|gehalt", re.I)
MONTHLY_RE = re.compile(r"/\s*mo\b|per month|a month|monthly|\bpm\b|mensual|monatlich|per månad", re.I)


def _salary_single(rng: str, monthly: bool) -> str | None:
    """'60000-80000' -> the midpoint as one number ('70000', or '5833' per month), for boxes that reject a range."""
    nums = [int(n) for n in re.findall(r"\d+", rng.replace(",", "").replace(".", ""))]
    if not nums:
        return None
    mid = sum(nums[:2]) / len(nums[:2])
    return str(round(mid / 12) if monthly else round(mid))


def _fill_verified(page: Page, loc, val: str, kind: str, typ: str) -> bool:
    """Fill, read back, retry by typing (needed for phone widgets that reset on .fill())."""
    digits = typ == "tel"
    try:
        loc.fill(val, timeout=4000)
        if kind != "text":
            return True
        page.wait_for_timeout(300)
        if _same(loc.input_value(), val, digits):
            return True
        loc.click(timeout=3000)
        loc.fill("", timeout=3000)
        loc.press_sequentially(val.replace(" ", ""), delay=40)
        loc.blur()
        page.wait_for_timeout(300)
        return _same(loc.input_value(), val, digits)
    except Exception:  # hidden/covered/autocomplete-only field: report "not filled" instead of crashing the run
        return False


APPLY_PATTERNS = [
    r"^\s*apply( now| here| today| for this (job|position|role))?\s*$",                                   # plain "Apply" / "Apply now"
    r"fill (out|in) (the |your )?(application|form)|application form|apply (manually|with (my )?(e-?mail|cv|resume))|start (your |the )?application|"
    r"begin (your )?application|continue (to|with) (the |your )?application|submit (your )?application|apply (for|to) (this|the)",
    r"apply|ansök|postuler|candidater|bewerb|solliciteer|aplicar|i'?m interested|jag \u00e4r intresserad|get started|interested in this|int\u00e9ress|interesad|interessiert|ge\u00efnteresseerd|interessato|je suis|enviar candidatura|candidatar|inscri",
]
NOT_APPLY = re.compile(r"linkedin|xing|dropbox|google|facebook|indeed|import|share|save|log ?in|sign ?(in|up)|alert|subscribe|follow|talent (network|community)|"
                       r"easy apply|back|privacy|legal|cookie", re.I)


def _form_showing(page) -> bool:
    """A real form is on screen when several inputs are VISIBLE (a hidden upload box doesn't count; newsletter widgets have 1-2 fields)."""
    try:
        return page.locator("input:visible, textarea:visible, select:visible").count() >= 3
    except Exception:
        return False


def _apply_target(page):
    for pat in APPLY_PATTERNS:
        for role in ("link", "button"):
            loc = page.get_by_role(role, name=re.compile(pat, re.I))
            for i in range(min(loc.count(), 8)):
                el = loc.nth(i)
                try:
                    name = ((el.inner_text() or el.get_attribute("aria-label") or "")).strip()
                    if el.is_visible() and not NOT_APPLY.search(name):
                        return el
                except Exception:
                    continue
    txt = page.get_by_text(re.compile(r"^\s*apply( now)?\s*$", re.I)).filter(visible=True)   # styled divs/spans acting as buttons
    return txt.first if txt.count() else None


def _wait_form(page, secs: int) -> bool:
    """Single-page apps build their form a moment after the page loads or a button is clicked: wait for it before judging."""
    for _ in range(secs):
        if _form_showing(page):
            return True
        page.wait_for_timeout(1000)
    return _form_showing(page)


def _open_form(page):
    """Click through Apply -> (choose a method) -> form, up to 3 steps. A click may navigate this tab or open a new one."""
    if _wait_form(page, 2):
        return page
    for _ in range(3):
        target = _apply_target(page)
        if target is None:
            break
        try:
            with page.context.expect_page(timeout=4000) as np_:
                target.click()
            page = np_.value
            page.wait_for_load_state("domcontentloaded", timeout=20000)
            page.set_default_timeout(12000)
        except Exception:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
        if _wait_form(page, 8):
            break
    return page


def _dismiss_cookies(page: Page):
    for name in ("Accept all", "Accept", "Allow all", "Godkänn", "Acceptera", "OK", "Got it"):
        b = page.get_by_role("button", name=re.compile(rf"^{name}", re.I))
        if b.count():
            try:
                b.first.click(timeout=1500)
                return
            except Exception:
                pass


def run(job: dict, submit: bool) -> Result:
    qa, missing = [], []
    assist = env("ASSIST", "0") == "1"  # visible window; a human completes the CAPTCHA
    if "linkedin.com" in job["url"]:
        return Result("needs_input", "LinkedIn Easy Apply needs your login - apply manually from the link, then mark it applied here")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel=env("BROWSER_CHANNEL", "msedge"), headless=(env("HEADLESS", "1") == "1") and not assist,
                                  slow_mo=int(env("SLOW_MO", "0")))
        from . import sitelogin
        _st = sitelogin.state_for(job["url"])  # your saved login for Internshala / Unstop, if any
        page = (browser.new_context(storage_state=str(_st), viewport={"width": 1280, "height": 1800}).new_page() if _st
                else browser.new_page(viewport={"width": 1280, "height": 1800}))
        page.set_default_timeout(12000)  # fail fast instead of 30s hangs
        shot = SCREENS / (re.sub(r"\W+", "_", job["id"])[:60] + ".png")
        try:
            page.goto(job["url"], wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2000)
            _dismiss_cookies(page)
            page = _open_form(page)  # Apply now -> (choose a method) -> the real form, following new tabs if needed
            # Teamtailor-style job pages keep the real form at <job url>/applications/new
            if page.locator("input[type=file]").count() == 0 and re.search(r"/jobs/\d+-", page.url) and "/applications" not in page.url:
                try:
                    page.goto(page.url.split("?")[0].rstrip("/") + "/applications/new", wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(2500)
                except Exception:
                    pass
            def closed() -> bool:
                try:
                    return bool(CLOSED_RE.search(page.inner_text("body")[:4000]))
                except Exception:
                    return False

            if closed():  # maybe our /applications/new guess hit a 404 on a live job: retry the original link once
                try:
                    page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(2000)
                except Exception:
                    pass
                if closed():
                    page.screenshot(path=str(shot))
                    return Result("needs_input", "posting appears closed or removed - nothing to apply to", screenshot=str(shot))
            try:  # Cloudflare / similar interstitial: a bot check, i.e. a CAPTCHA we hand back to you
                bot_wall = re.search(r"just a moment|attention required|verify you are human|checking your browser|ddos protection", page.title() + " " + page.inner_text("body")[:400], re.I)
            except Exception:
                bot_wall = None
            try:  # the site is throttling us (HTTP 429): that is "try again later", not "no form"
                throttled = re.search(r"error 429|too many requests|rate limit", page.title() + " " + page.inner_text("body")[:500], re.I)
            except Exception:
                throttled = None
            if throttled:
                page.screenshot(path=str(shot))
                return Result("needs_input", "site is rate-limiting us (429 too many requests) - will work on a later retry", screenshot=str(shot))
            if (bot_wall or (_has_captcha(page) and not _form_showing(page))) and not assist:  # try the solver service before giving up
                from . import captchasolve
                if captchasolve.enabled() and captchasolve.solve(page):
                    page.wait_for_timeout(5000)
                    try:
                        bot_wall = re.search(r"just a moment|attention required|verify you are human|checking your browser|ddos protection", page.title() + " " + page.inner_text("body")[:400], re.I)
                    except Exception:
                        pass
            if bot_wall and not assist:
                page.screenshot(path=str(shot))
                return Result("needs_input", "CAPTCHA / bot check (Cloudflare) - apply manually from the link", screenshot=str(shot))
            # Invisible reCAPTCHA / Turnstile widgets sit on most forms and need no human: only stop up front when there is a challenge
            # and no form to fill. A real challenge at Submit is still caught below ("CAPTCHA at submit") and left for assist mode.
            if _has_captcha(page) and not assist and not _form_showing(page):
                page.screenshot(path=str(shot))
                return Result("needs_input", "CAPTCHA / bot check present - apply manually from the link", screenshot=str(shot))

            if not job.get("allow_senior") and (why := _title_blocked(page)):
                return Result("needs_input", why, screenshot=str(shot))

            scope, fields = page, []  # the form may live inside an embedded frame (e.g. Greenhouse on a company site)
            for fr in page.frames:
                try:
                    got = fr.evaluate(SCAN_JS)
                except Exception:
                    continue
                if got:
                    scope, fields = fr, got
                    break
            if not fields:
                page.screenshot(path=str(shot))
                return Result("failed", "no form fields found - check screenshot", screenshot=str(shot))
            # Guard: a real application has a CV upload or several fields. A lone email box is a newsletter/referral widget.
            if not any(f["kind"] == "file" for f in fields) and len(fields) < 4:
                page.screenshot(path=str(shot))
                return Result("needs_input", f"no real application form found (only {len(fields)} small field(s), no CV upload) - check screenshot",
                              screenshot=str(shot))

            def L(f: dict, opt: dict | None = None):
                """Locator for a field/option; if the page re-rendered (random ids), re-scan and re-find it by label."""
                sel = (opt or f)["sel"]
                if scope.locator(sel).count() == 0:
                    for g in scope.evaluate(SCAN_JS):
                        if g["kind"] == f["kind"] and g["label"] == f["label"]:
                            sel = next((o["sel"] for o in g.get("options", []) if opt and o["label"] == opt["label"]), g["sel"] if not opt else sel)
                            break
                return scope.locator(sel)

            file_fields = [x for x in fields if x["kind"] == "file"]

            def _upload(f, path):
                """Upload to this field; if the page re-rendered and its marker now points at something else, use the n-th real file input."""
                try:
                    L(f).set_input_files(path, timeout=6000)
                except Exception:
                    files = scope.locator("input[type=file]")
                    if not files.count():
                        raise
                    files.nth(min(file_fields.index(f), files.count() - 1)).set_input_files(path, timeout=6000)

            # this job's tailored resume if the agent made one (daemon sets resume_path), else your original
            rp = Path(job["resume_path"]) if job.get("resume_path") and Path(job["resume_path"]).exists() else _resume()
            cp = _cover_pdf()
            resume_done = email_done = False
            email_present = any(f["kind"] == "text" and f.get("type") == "email" for f in fields)
            file_present = any(f["kind"] == "file" for f in fields)
            for f in fields:
                q, kind = f["label"], f["kind"]
                if kind == "file":
                    ql = (q or "").lower()
                    is_resume = re.search(r"resume|\bcv\b|curriculum|lebenslauf", ql) is not None
                    is_cover = re.search(r"cover|letter|brev|motivation|anschreiben", ql) is not None
                    is_extra = re.search(r"additional|other|supporting|attach|portfolio|certificate|transcript", ql) is not None
                    if re.search(r"autofill|auto-fill|import", ql):
                        continue  # 'autofill from resume' boxes re-write the fields we fill ourselves
                    if is_cover and cp:
                        _upload(f, str(cp)); qa.append((q, cp.name, "file"))
                    elif rp and (is_resume or not (is_extra or is_cover)):  # every resume box (some forms have two) or an unlabeled upload box
                        _upload(f, str(rp)); qa.append((q or "resume", rp.name, "file")); resume_done = True
                    elif cp and is_extra and not is_resume:
                        _upload(f, str(cp)); qa.append((q, cp.name, "file"))
                    elif f["required"]:
                        missing.append(q or "file upload")
                elif kind == "checkbox":
                    if re.search(r"newsletter|marketing|promotional|receive (our |tairc |research )?(updates|offers|emails|news)|research updates|subscribe|future (job )?opportunit|contact me (about|regarding) (other|future)|sms|text message|talent (network|community)", (q or "") + " " + f.get("group", ""), re.I):
                        continue  # optional marketing opt-in: leave unticked
                    if re.search(r"privacy|terms|consent|gdpr|agree|accept|certify|acknowledg|i declare|attest|i understand|i have read|authori[sz]e|integritet|samtycke|godkänn|personuppgift|behandl|datenschutz|einwillig|akzeptier|zustimm|"
                                 r"personal (data|information)|data (will be |is |are )?(processed|stored|held)|processing of|data protection|persoonsgegevens|verwerk|akkoord|"
                                 r"dati personali|donn[ée]es personnelles|datos personales|j'accepte|acepto|accetto|pers[öo]nliche daten|dine data|dina uppgifter", q, re.I):
                        if _check_box(scope, f):
                            qa.append((q, "checked", "consent"))
                        elif f["required"] or re.search(r"privacy|gdpr|datenschutz|integritet|personuppgift|personal data", q, re.I):
                            missing.append(f"consent box not ticked: {q[:50]}")
                    elif re.search(r"var vill du|where (would|do) you|which office|office|kontor|location|city|stad|standort|vestiging", f.get("group", ""), re.I):
                        if _check_box(scope, f):
                            qa.append((f"{f.get('group', '')[:30]}: {q}", "checked", "relocation-ok"))  # open to relocation
                    elif f["required"]:
                        missing.append(q)
                elif kind in ("radio", "select"):
                    o, ans, via = _pick(f, job)
                    if o is None:
                        if f["required"]:
                            missing.append(f"{q} [{via}]")
                        continue
                    if kind == "select":
                        L(f).select_option(value=o["id"])
                    elif not _click_radio(scope, f, o):
                        if f["required"]:
                            missing.append(f"{q} [could not select '{o['label']}']")
                        continue
                    qa.append((q, o["label"], via))
                else:  # text / textarea
                    if re.search(r"newsletter|subscribe|subscription", (q or "") + " " + f.get("sel", ""), re.I):
                        continue  # a footer sign-up box, not part of the application: never put your email in it
                    val, via = _personal(q, f.get("type", "")), "profile"
                    if f.get("type") == "date" or re.search(r"start date|available (from|to start)|earliest (start|available)|when can you start", q, re.I):
                        # You're immediately available: a real date box needs an actual date, a text box can say it in words.
                        val = (date.today() + timedelta(days=14)).isoformat() if f.get("type") == "date" else profile()["notice_period"]
                    if val is None and kind == "textarea" and re.search(r"cover|letter|motivation|brev|anschreiben|lettre", q, re.I):
                        val, via = _cover_text(job), "cover_pdf"
                    if val is None and (f["required"] or kind == "textarea"):
                        val, via = rag.answer(q, job)
                        if via == "NEEDS_USER":
                            if f["required"]:
                                missing.append(q)
                            continue
                    if val and SALARY_RE.search(q or ""):
                        monthly = bool(MONTHLY_RE.search(q or ""))
                        single = _salary_single(val, monthly)
                        if single and (monthly or f.get("type") == "number"):
                            val = single  # a monthly or numeric box can't take "60000-80000"
                        elif single and kind == "text" and not _fill_verified(page, L(f), val, kind, f.get("type", "")):
                            val = single  # the box rejected the range: give one number instead
                    if val and kind == "text" and re.search(r"currently located|where are you (currently )?(based|located)|current location", q, re.I):
                        try:  # typeahead box: the value only counts once a suggestion is picked from the list
                            loc = L(f)
                            loc.click(timeout=3000)
                            loc.fill("", timeout=3000)
                            loc.press_sequentially(val.split(",")[0], delay=60)
                            page.wait_for_timeout(1500)
                            sug = scope.locator("[role=option]:visible").first
                            if sug.count():
                                sug.click(timeout=2500)
                                qa.append((q, val, via))
                                continue
                        except Exception:
                            pass
                    if val:
                        if _fill_verified(page, L(f), val, kind, f.get("type", "")):
                            qa.append((q, val[:200], via))
                            email_done = email_done or val == profile()["email"]
                        elif f["required"]:
                            missing.append(f"{q} [page rejected the value]")

            try:  # Yes/No button pairs: answer from your rules/resume (this also overwrites a value the site's own autofill put there)
                yn_groups = scope.evaluate(YESNO_JS)
            except Exception:
                yn_groups = []
            for g in yn_groups:
                gq = g["label"].replace("*", "").strip()
                yf = {"label": gq, "kind": "radio", "required": g["required"], "options": [{"label": "Yes", "sel": ""}, {"label": "No", "sel": ""}]}
                o, ans, via = _pick(yf, job)
                if o is None:
                    if g["required"]:
                        missing.append(f"{gq} [{via}]")
                    continue
                try:
                    scope.locator(f'[data-ja-yn="{g["id"]}"]').get_by_role("button", name=re.compile(rf"^{o['label']}$", re.I)).click(timeout=4000)
                    qa.append((gq, o["label"], via))
                except Exception:
                    if g["required"]:
                        missing.append(f"{gq} [could not click '{o['label']}']")

            unreadable = [f for f in fields if f["kind"] in ("text", "textarea") and not f["label"].strip()]
            if unreadable:  # never claim a form is filled when we couldn't tell what some boxes are
                missing.append(f"{len(unreadable)} text field(s) with unreadable labels")
            if email_present and not email_done:
                missing.append("email not filled")
            if file_present and rp and not resume_done:
                missing.append("resume not uploaded")
            try:  # never submit while a file is still uploading
                page.wait_for_function("!/uploading/i.test(document.body.innerText)", timeout=45000)
            except Exception:
                pass  # the word "uploading" can sit on the page as static text; the submit verdict below tells the truth
            page.wait_for_timeout(1000)
            _safe_shot(page, shot)
            if missing:
                return Result("needs_input", "unanswered required: " + " | ".join(missing), qa, str(shot))
            if not submit:
                return Result("dry_run_ok", "filled, not submitted", qa, str(shot))

            sub = page.locator("button[type=submit], input[type=submit]").or_(
                page.get_by_role("button", name=re.compile(r"^(submit|send|apply|skicka|ansök|solliciteer|bewerb|postuler|candidat|enviar|inviare|verstuur|absenden)", re.I)))
            sub = sub.filter(has_not_text=re.compile(r"alert|subscribe|sign ?up|notify me|newsletter", re.I))  # never "Create Alert" / "Sign up" as if it were Submit
            if not sub.count():
                return Result("needs_input", "no application form here: the only button is a job-alert / sign-up one", qa, str(shot))
            if not _click_submit(page, sub):
                return Result("failed", "submit button could not be clicked", qa, str(shot))
            page.wait_for_timeout(2500)
            # Some sites ask a follow-up in a pop-up after Submit (e.g. "Are you willing to relocate?"). You're open to relocating.
            for _ in range(2):
                yes = page.get_by_role("button", name=re.compile(r"^yes\b.*relocat", re.I))
                if yes.count() and yes.first.is_visible():
                    label = yes.first.inner_text()[:60]  # read before clicking: the pop-up closes afterwards
                    yes.first.click()
                    qa.append(("relocation pop-up", label, "relocation-ok"))
                    page.wait_for_timeout(3000)
            page.wait_for_timeout(2500)

            def verdict() -> tuple[bool, str]:
                body = _body_text(page).lower()
                page.screenshot(path=str(shot), full_page=True)
                invalid = page.locator("[aria-invalid='true']:visible, [role='alert']:visible").filter(has_text=re.compile(r"\S")).count() \
                    + page.locator("[aria-invalid='true']:visible").count()
                # Invalid fields only matter while OUR form is still on screen. After a real submit, thank-you pages often show an unrelated
                # form (feedback, newsletter, create-a-profile) whose empty required box must not turn a success into a "failure".
                try:
                    ours_still_there = page.evaluate("(email) => { " + DEEP_JS + " return deep('input').some(i => i.value === email && i.getClientRects().length > 0); }",
                                                     profile()["email"])
                except Exception:
                    ours_still_there = True
                if not ours_still_there:
                    invalid = 0
                pending_q = page.get_by_role("button", name=re.compile(r"^(yes|no)\b", re.I)).filter(visible=True).count() > 0 \
                    and page.get_by_role("dialog").count() > 0
                return detect_submission(body, invalid, pending_q)

            ok, evidence = verdict()
            if not ok and not assist:  # a CAPTCHA blocked the submit: solve it through the solver service, then submit again
                from . import captchasolve
                if captchasolve.enabled():
                    for _ in range(2):
                        if not captchasolve.solve(page):
                            break
                        qa.append(("captcha", "solved by solver service", "auto"))
                        page.wait_for_timeout(1500)
                        _click_submit(page, sub)
                        page.wait_for_timeout(5000)
                        ok, evidence = verdict()
                        if ok:
                            break
            if assist and not ok:  # a human step (CAPTCHA) is probably showing: wait for YOU to complete it in the window
                print(">>> Solve the CAPTCHA / press Submit in the browser window. Waiting up to 10 minutes...", flush=True)
                end = time.time() + 600
                while not ok and time.time() < end:
                    page.wait_for_timeout(4000)
                    ok, evidence = verdict()
            if ok:
                return Result("submitted", evidence, qa, str(shot))
            if any(k in fr.url for fr in page.frames for k in ("challenges.cloudflare.com", "hcaptcha", "recaptcha", "arkoselabs", "captcha-delivery")):
                return Result("needs_input", "CAPTCHA at submit (the form is fully filled) - finish it with: python -m jobagent assist", qa, str(shot))
            return Result("failed", f"NOT confirmed ({evidence}) - check screenshot", qa, str(shot))
        except Exception as e:
            try:
                page.screenshot(path=str(shot), full_page=True)
            except Exception:
                pass
            return Result("failed", f"{type(e).__name__}: {e}"[:300], qa, str(shot))
        finally:
            browser.close()


def _safe_shot(page, shot):
    """A screenshot is evidence, never a reason to fail an application (heavy pages time out on full_page)."""
    for full in (True, False):
        try:
            page.screenshot(path=str(shot), full_page=full, timeout=8000)
            return
        except Exception:
            continue


def _click_submit(page, sub) -> bool:
    """Click the real submit control: visible + enabled ones only (a hidden duplicate or cookie banner must not eat the click), with fallbacks."""
    cands = [sub.nth(i) for i in range(min(sub.count(), 12))]
    vis = []
    for b in cands:
        try:
            if b.is_visible() and b.is_enabled():
                vis.append(b)
        except Exception:
            pass
    for b in reversed(vis or cands):
        for how in ("normal", "force", "js"):
            try:
                b.scroll_into_view_if_needed(timeout=4000)
                if how == "normal":
                    b.click(timeout=6000)
                elif how == "force":
                    b.click(timeout=4000, force=True)
                else:
                    b.evaluate("e => e.click()")
                return True
            except Exception:
                continue
    try:  # last resort: ask the form to submit itself
        return bool(page.evaluate("() => { const f = document.querySelector('form'); if (!f) return false; f.requestSubmit ? f.requestSubmit() : f.submit(); return true; }"))
    except Exception:
        return False


# Share the shadow-DOM-aware search helper with every in-page script (all three are defined above by now).
for _n in ("SCAN_JS", "CLICK_OPTION_JS", "CLICK_CHECKBOX_JS"):
    globals()[_n] = globals()[_n].replace("%DEEP%", DEEP_JS)
