"""Automatic CAPTCHA solving through a solver service (2Captcha-compatible API: 2captcha.com, or any service speaking the same in.php/res.php protocol).

Enabled only when CAPTCHA_API_KEY is set in .env (the service charges roughly $1-3 per 1000 solves; there is no reliable free solver).
`solve(page)` finds the widget on the page, gets a token from the service, writes it into the form and fires the widget callback.
Returns True when a token was injected; the caller then re-submits and re-checks for confirmation.
"""
import logging
import time

import httpx

from .config import env

log = logging.getLogger(__name__)
API = "https://2captcha.com"
KIND = {"recaptcha": "userrecaptcha", "hcaptcha": "hcaptcha", "turnstile": "turnstile"}

FIND_JS = r"""() => {
  const out = {};
  const el = document.querySelector('.g-recaptcha[data-sitekey], [data-sitekey].g-recaptcha');
  if (el) { out.kind = 'recaptcha'; out.sitekey = el.getAttribute('data-sitekey'); out.callback = el.getAttribute('data-callback'); return out; }
  const h = document.querySelector('.h-captcha[data-sitekey]'); if (h) { out.kind = 'hcaptcha'; out.sitekey = h.getAttribute('data-sitekey'); return out; }
  const t = document.querySelector('.cf-turnstile[data-sitekey]'); if (t) { out.kind = 'turnstile'; out.sitekey = t.getAttribute('data-sitekey'); return out; }
  for (const f of document.querySelectorAll('iframe[src]')) {
    const s = f.src, m = s.match(/[?&](?:k|sitekey)=([\w-]+)/);
    if (/recaptcha/.test(s) && m) { out.kind = 'recaptcha'; out.sitekey = m[1]; out.enterprise = /enterprise/.test(s); out.invisible = /size=invisible/.test(s); return out; }
    if (/hcaptcha/.test(s)) { const q = s.match(/sitekey=([\w-]+)/); if (q) { out.kind = 'hcaptcha'; out.sitekey = q[1]; return out; } }
    if (/challenges\.cloudflare/.test(s)) { const q = s.match(/\/(0x[\w-]+)\//); if (q) { out.kind = 'turnstile'; out.sitekey = q[1]; return out; } }
  }
  for (const sc of document.querySelectorAll('script[src*="recaptcha"]')) {
    const m = sc.src.match(/render=([\w-]+)/); if (m && m[1] !== 'explicit') { out.kind = 'recaptcha'; out.sitekey = m[1]; out.enterprise = /enterprise/.test(sc.src); out.v3 = true; return out; }
  }
  return null;
}"""

INJECT_JS = r"""(a) => {
  const tok = a.token;
  for (const sel of ['textarea[name="g-recaptcha-response"]', '#g-recaptcha-response', 'textarea[name="h-captcha-response"]', 'textarea[name="g-recaptcha-response-100000"]',
                     'input[name="cf-turnstile-response"]', 'textarea[name="cf-turnstile-response"]']) {
    document.querySelectorAll(sel).forEach(e => { e.value = tok; e.innerHTML = tok; e.dispatchEvent(new Event('input', {bubbles: true})); e.dispatchEvent(new Event('change', {bubbles: true})); });
  }
  try {  // fire the widget callback, which is what actually tells the form the CAPTCHA is solved
    const cfg = window.___grecaptcha_cfg;
    if (cfg && cfg.clients) for (const c of Object.values(cfg.clients)) {
      const walk = (o, d) => { if (!o || d > 4 || typeof o !== 'object') return; for (const v of Object.values(o)) { if (v && typeof v === 'object' && typeof v.callback === 'function') { try { v.callback(tok); } catch (e) {} } else walk(v, d + 1); } };
      walk(c, 0);
    }
    if (a.callback && typeof window[a.callback] === 'function') window[a.callback](tok);
  } catch (e) {}
  return true;
}"""


def enabled() -> bool:
    return bool(env("CAPTCHA_API_KEY", ""))


def _token(kind: str, sitekey: str, url: str, extra: dict) -> str | None:
    key = env("CAPTCHA_API_KEY", "")
    data = {"key": key, "method": KIND[kind], "pageurl": url, "json": 1}
    data["sitekey" if kind != "recaptcha" else "googlekey"] = sitekey
    data.update(extra)
    r = httpx.post(f"{API}/in.php", data=data, timeout=30).json()
    if r.get("status") != 1:
        log.info("captcha: solver refused the task: %s", r)
        return None
    rid = r["request"]
    end = time.time() + 150
    while time.time() < end:
        time.sleep(5)
        g = httpx.get(f"{API}/res.php", params={"key": key, "action": "get", "id": rid, "json": 1}, timeout=30).json()
        if g.get("status") == 1:
            return g["request"]
        if g.get("request") != "CAPCHA_NOT_READY":
            log.info("captcha: solver failed: %s", g)
            return None
    return None


def solve(page) -> bool:
    if not enabled():
        return False
    info = None
    for fr in page.frames:  # the widget can live in the page or in an embedded application frame
        try:
            info = fr.evaluate(FIND_JS)
        except Exception:
            continue
        if info and info.get("sitekey"):
            break
    if not info or not info.get("sitekey") or info["kind"] not in KIND:
        log.info("captcha: no solvable widget found on %s (frames: %s)", page.url[:80], [f.url[:60] for f in page.frames if 'captcha' in f.url][:3])
        return False
    if info.get("enterprise"):  # score-based Enterprise reCAPTCHA: workers report it unsolvable, so do not burn a minute and money on it
        log.info("captcha: enterprise reCAPTCHA on %s skipped (unsolvable)", page.url[:60])
        return False
    extra = {}
    if info["kind"] == "recaptcha":
        if info.get("enterprise"):
            extra["enterprise"] = 1
        if info.get("v3"):
            extra.update(version="v3", action="submit", min_score="0.7")
        elif info.get("invisible"):
            extra["invisible"] = 1
    tok = _token(info["kind"], info["sitekey"], page.url, extra)
    log.info("captcha: %s %s extra=%s -> token %s", info["kind"], info["sitekey"][:12], extra, "OK" if tok else "NONE")
    if not tok:
        return False
    for fr in page.frames:
        try:
            fr.evaluate(INJECT_JS, {"token": tok, "callback": info.get("callback")})
        except Exception:
            pass
    return True
