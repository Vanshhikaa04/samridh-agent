"""LinkedIn Easy Apply through YOUR logged-in session (the multi-step pop-up). Same rules as the rest of the agent:
answers come only from your resume/profile/rules; anything it can't answer truthfully makes it stop and park the job.
Capped per day (EASY_APPLY_MAX_PER_DAY). Needs `linkedin-login` done and USE_LINKEDIN_LOGIN=1.
"""
import re
import time
import random
from datetime import date

from . import lilogin, rag
from .apply import Result, _pick, _personal, SCREENS
from .config import env, profile

SUCCESS_RE = re.compile(r"application sent|your application was sent|application submitted", re.I)
LABEL_JS = """e => {
  const lab = e.id && document.querySelector(`label[for="${CSS.escape(e.id)}"]`);
  const fs = e.closest('fieldset'); const lg = fs && fs.querySelector('legend');
  const t = (lab && lab.innerText) || e.getAttribute('aria-label') || (lg && lg.innerText) || e.placeholder || '';
  return t.replace(/\\s+/g, ' ').trim();
}"""


def _cap_ok() -> bool:
    s = lilogin._state()
    return not (s.get("easy_day") == str(date.today()) and s.get("easy_count", 0) >= int(env("EASY_APPLY_MAX_PER_DAY", "5")))


def _fill_radio_groups(dlg, job, qa, missing):
    """Radio questions. LinkedIn's current layout: a role=radiogroup whose options are role=radio divs; the question text is the options'
    aria-label and the visible answer ("Yes"/"No") is a paragraph inside each. Older layouts used <fieldset><legend>. Both are handled.
    The answer comes from your rules/resume (never guessed); the visible option is clicked."""
    for grp in dlg.locator("[role=radiogroup]").all():
        try:
            radios = grp.locator("[role=radio]")
            n = radios.count()
            if n == 0:
                continue
            if any(radios.nth(k).get_attribute("aria-checked") == "true" for k in range(n)):
                continue  # already answered (pre-filled by LinkedIn)
            q = (radios.first.get_attribute("aria-label") or "").strip()
            if not q and grp.locator("legend").count():
                q = max([x.strip() for x in grp.locator("legend").first.inner_text().split(chr(10)) if x.strip()] or [""], key=len)
            labels = [re.sub(r"\s+", " ", radios.nth(k).inner_text()).strip() for k in range(n)]
            if not q or not all(labels):
                continue
            opts = [{"id": str(k), "label": lab} for k, lab in enumerate(labels)]
            o, ans, via = _pick({"label": q, "options": opts, "kind": "radio", "required": True}, job)
            if o is None:
                missing.append(f"{q[:80]} [{via}]")
                continue
            radios.nth(int(o["id"])).click()
            qa.append((q, o["label"], via))
        except Exception:
            continue


def _fill_step(dlg, job, qa, missing):
    """Fill every visible control in the current Easy Apply step."""
    _fill_radio_groups(dlg, job, qa, missing)
    seen_radio = set()
    for el in dlg.locator("input:not([type=hidden]):visible, textarea:visible, select:visible").all():
        try:
            tag = el.evaluate("e => e.tagName.toLowerCase()")
            typ = (el.get_attribute("type") or tag).lower()
            label = el.evaluate(LABEL_JS)
            required = (el.get_attribute("required") is not None) or el.get_attribute("aria-required") == "true" or "required" in label.lower()
            if typ == "file":
                continue  # LinkedIn reuses the resume saved on your profile
            if typ == "checkbox":
                if re.search(r"follow|newsletter|marketing|updates|alerts|promotion|share my|talent (community|network)", label, re.I):
                    continue  # leave 'follow company' etc. unticked
                near = el.evaluate("e => ((e.closest('fieldset, [data-test-form-element], .fb-dash-form-element') || e.parentElement.parentElement || e).innerText || '').slice(0, 400)")
                if (re.search(r"consent|agree|accept|privacy|terms|gdpr|data protection|certify|confirm|declare|acknowledg|personal data|i have read|i understand", label + " " + near, re.I)
                        or required):
                    if not el.is_checked():
                        el.evaluate("e => (e.closest('label') || document.querySelector(`label[for=\"${e.id}\"]`) || e).click()")
                        if not el.is_checked():
                            el.check(force=True)
                    qa.append((label[:80] or "consent", "checked", "consent"))
                continue
            if typ == "radio":
                name = el.get_attribute("name") or label
                if name in seen_radio:
                    continue
                seen_radio.add(name)
                group = dlg.locator(f'input[type=radio][name="{name}"]')
                opts = []
                for i in range(group.count()):
                    r = group.nth(i)
                    lab = r.evaluate("e => { const l = e.id && document.querySelector(`label[for=\"${CSS.escape(e.id)}\"]`); return ((l && l.innerText) || e.value || '').trim() }")
                    opts.append({"id": str(i), "label": lab})
                q = el.evaluate("e => { const fs = e.closest('fieldset'); const lg = fs && fs.querySelector('legend'); return ((lg && lg.innerText) || '').replace(/\\s+/g,' ').trim() }") or label
                o, ans, via = _pick({"label": q, "options": opts, "kind": "radio", "required": required}, job)
                if o is None:
                    if required:
                        missing.append(f"{q[:70]} [{via}]")
                    continue
                dlg.locator(f'input[type=radio][name="{name}"]').nth(int(o["id"])).evaluate("e => (e.closest('label') || document.querySelector(`label[for=\"${e.id}\"]`) || e).click()")
                qa.append((q, o["label"], via))
            elif tag == "select":
                opts = [{"id": v, "label": t.strip()} for v, t in el.evaluate("e => [...e.options].filter(o => o.value && !/^select/i.test(o.text)).map(o => [o.value, o.text])")]
                cur = el.input_value()
                if cur and not re.match(r"select", el.evaluate("e => e.options[e.selectedIndex].text"), re.I):
                    continue  # already set by LinkedIn from your profile
                o, ans, via = _pick({"label": label, "options": opts, "kind": "select", "required": required}, job)
                if o is None:
                    if required:
                        missing.append(f"{label[:70]} [{via}]")
                    continue
                el.select_option(value=o["id"])
                qa.append((label, o["label"], via))
            else:  # text / number / textarea
                if el.input_value().strip():
                    continue  # prefilled (contact info)
                val, via = _personal(label, typ), "profile"
                if val is None and re.search(r"years? of (work )?experience|how many years", label, re.I) and not re.search(r"\bwith\b|\bin\b|using", label, re.I):
                    val = str(profile()["years_experience"])
                if val is None:
                    val, via = rag.answer(label, job)
                    if via == "NEEDS_USER":
                        if required:
                            missing.append(label[:70])
                        continue
                if typ == "number":
                    m = re.search(r"\d+(?:\.\d+)?", val)
                    val = m.group(0) if m else val
                el.fill(val[:500])
                qa.append((label, val[:120], via))
        except Exception:
            continue


def apply(job: dict, submit: bool) -> Result:
    from .lilogin import BROWSER_LOCK
    with BROWSER_LOCK:
        return _apply(job, submit)


def _apply(job: dict, submit: bool) -> Result:
    ok, why = lilogin.enabled()
    if not ok:
        return Result("needs_input", f"Easy Apply not available: {why}")
    if not _cap_ok():
        return Result("deferred", f"deferred: Easy Apply daily cap ({env('EASY_APPLY_MAX_PER_DAY', '5')}/day, protects your LinkedIn account) reached - will retry tomorrow")
    m = re.search(r"(\d{6,})(?:\?|$)", job["url"].split("?")[0] + "?")
    if not m:
        return Result("needs_input", "no LinkedIn job id")
    from playwright.sync_api import sync_playwright
    qa, missing = [], []
    shot = SCREENS / f"easy_{m.group(1)}.png"
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(lilogin.PROFILE), channel=env("BROWSER_CHANNEL", "msedge"),
                                                    headless=env("LINKEDIN_HEADLESS", "1") == "1", viewport={"width": 1200, "height": 1400})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(12000)
        try:
            time.sleep(random.uniform(3, 7))
            page.goto(f"https://www.linkedin.com/jobs/view/{m.group(1)}/", wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            if re.search(r"login|checkpoint|authwall|challenge|captcha", page.url):
                return Result("needs_input", "LinkedIn asked for login/verification - re-run linkedin-login")
            btn = page.get_by_role("button", name=re.compile(r"easy apply", re.I))
            if not btn.count():
                return Result("needs_input", "no Easy Apply button (closed, or external apply)", screenshot=str(shot))
            btn.first.click()
            page.wait_for_timeout(2500)
            clicked = False  # True once "Submit application" was pressed
            for _ in range(12):
                dlg = page.get_by_role("dialog").first
                body = dlg.inner_text() if dlg.count() else ""
                if not body and clicked:  # after Submit LinkedIn closes the pop-up and shows "Application status: Application submitted" on the job page
                    body = page.inner_text("body")[:8000]
                if SUCCESS_RE.search(body):
                    page.screenshot(path=str(shot))
                    s = lilogin._state()
                    lilogin._save(easy_day=str(date.today()), easy_count=(s.get("easy_count", 0) if s.get("easy_day") == str(date.today()) else 0) + 1)
                    return Result("submitted", f"LinkedIn confirmation: '{SUCCESS_RE.search(body).group(0)}'", qa, str(shot))
                _fill_step(dlg, job, qa, missing)
                if missing:
                    page.screenshot(path=str(shot))
                    return Result("needs_input", "Easy Apply needs answers I can't give truthfully: " + " | ".join(missing), qa, str(shot))
                submit_btn = dlg.get_by_role("button", name=re.compile(r"^submit application", re.I))
                if submit_btn.count():
                    if not submit:
                        page.screenshot(path=str(shot))
                        return Result("dry_run_ok", "Easy Apply filled up to the final Submit; not submitted", qa, str(shot))
                    follow = dlg.get_by_label(re.compile(r"follow", re.I))
                    if follow.count() and follow.first.is_checked():
                        follow.first.uncheck()
                    submit_btn.first.click()
                    clicked = True
                    page.wait_for_timeout(3500)
                    continue
                nxt = dlg.get_by_role("button", name=re.compile(r"^(next|review|continue)", re.I))
                if not nxt.count():
                    break
                nxt.first.click()
                page.wait_for_timeout(1800)
                err = dlg.locator("[role=alert]:visible, .artdeco-inline-feedback--error:visible")
                if err.count() and re.search(r"\S", err.first.inner_text()):
                    page.screenshot(path=str(shot))
                    return Result("needs_input", f"Easy Apply step rejected: {err.first.inner_text()[:100]}", qa, str(shot))
            page.screenshot(path=str(shot))
            return Result("failed", "NOT confirmed (Easy Apply did not reach 'Application sent') - check screenshot", qa, str(shot))
        except Exception as e:
            try:
                page.screenshot(path=str(shot))
            except Exception:
                pass
            return Result("failed", f"{type(e).__name__}: {str(e)[:150]}", qa, str(shot))
        finally:
            ctx.close()
