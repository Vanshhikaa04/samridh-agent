// Runs in the page (every frame) when you click the extension icon. Finds the form fields, asks your local agent what to put in each,
// fills what it can, and marks what it left for you. It never clicks Submit and never touches CAPTCHAs.
(async () => {
  if (window.__jaRunning) return;
  window.__jaRunning = true;
  const send = (m) => new Promise((res) => chrome.runtime.sendMessage(m, (r) => res(r || {})));
  const text = (s) => (s || "").replace(/\s+/g, " ").trim();
  const esc2 = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // ---- find every control, including inside open shadow roots
  function deep(sel, root = document, out = []) {
    root.querySelectorAll(sel).forEach((e) => out.push(e));
    root.querySelectorAll("*").forEach((e) => { if (e.shadowRoot) deep(sel, e.shadowRoot, out); });
    return out;
  }
  const visible = (e) => {
    if (!e.getClientRects().length) return false;
    const s = getComputedStyle(e);
    return s.visibility !== "hidden" && s.display !== "none" && e.type !== "hidden";
  };
  function labelOf(e) {
    const root = e.getRootNode();
    let t = "";
    if (e.id) { const l = root.querySelector(`label[for="${CSS.escape(e.id)}"]`); if (l) t = l.innerText; }
    if (!t) { const l = e.closest("label"); if (l) t = l.innerText; }
    if (!t && e.getAttribute("aria-labelledby")) t = e.getAttribute("aria-labelledby").split(/\s+/).map((i) => (root.getElementById ? root.getElementById(i) : document.getElementById(i))?.innerText || "").join(" ");
    if (!t) t = e.getAttribute("aria-label") || "";
    if (!t) { // nearest wrapper that carries a caption
      let p = e.parentElement;
      for (let i = 0; i < 4 && p && !t; i++, p = p.parentElement) {
        const cap = p.querySelector("label, legend, [class*=label], [class*=Label], [class*=title]");
        if (cap && cap.innerText && !cap.contains(e)) t = cap.innerText;
      }
    }
    if (!t) t = e.placeholder || e.name || "";
    return text(t).replace(/\*|\(required\)|required/gi, "").trim();
  }
  const groupOf = (e) => { const fs = e.closest("fieldset"); const lg = fs && fs.querySelector("legend"); return text(lg ? lg.innerText : ""); };
  const isRequired = (e) => e.required || e.getAttribute("aria-required") === "true" || /\*|required/i.test(e.closest("label, div")?.innerText?.slice(0, 200) || "");

  const fields = [], els = {};
  let n = 0;
  const radioGroups = {};
  for (const e of deep("input, textarea, select")) {
    const t = (e.type || e.tagName).toLowerCase();
    if (["hidden", "submit", "button", "image", "reset", "password", "search"].includes(t)) continue;
    if (t !== "file" && !visible(e)) continue;
    if (t === "file" && e.disabled) continue;
    if (e.disabled || e.readOnly) continue;
    if (t === "radio") {
      const key = e.name || labelOf(e);
      if (!radioGroups[key]) {
        const id = "f" + n++;
        radioGroups[key] = { id, kind: "radio", label: groupOf(e) || text(e.closest("fieldset, [role=radiogroup]")?.getAttribute("aria-label")) || key, required: isRequired(e), options: [], items: [] };
        fields.push(radioGroups[key]); els[id] = radioGroups[key];
      }
      const g = radioGroups[key];
      g.options.push({ id: String(g.items.length), label: labelOf(e) || e.value });
      g.items.push(e);
      continue;
    }
    if ((t === "text" || t === "email" || t === "tel" || t === "number" || t === "url" || t === "date" || t === "textarea") && e.value && e.value.trim()) continue;  // never overwrite what you typed
    const id = "f" + n++;
    const f = { id, kind: t === "file" ? "file" : t === "checkbox" ? "checkbox" : e.tagName === "SELECT" ? "select" : e.tagName === "TEXTAREA" ? "textarea" : "text",
                type: t, label: labelOf(e), group: groupOf(e), required: isRequired(e), el: e };
    if (f.kind === "select") {
      if (e.value && !/^\s*(select|choose|please|--)/i.test(e.options[e.selectedIndex]?.text || "")) continue;  // already chosen
      f.options = [...e.options].filter((o) => o.value && !/^\s*(select|choose|please|--)/i.test(o.text)).map((o) => ({ id: o.value, label: text(o.text) }));
    }
    if (f.kind === "checkbox" && e.checked) continue;
    fields.push(f); els[id] = f;
  }
  const payloadFields = fields.map(({ el, items, ...rest }) => rest);

  // ---- panel
  const host = document.createElement("div");
  host.style.cssText = "all:initial;position:fixed;right:16px;bottom:16px;z-index:2147483647";
  const sh = host.attachShadow({ mode: "open" });
  sh.innerHTML = `<style>
    .p{font:13px/1.45 system-ui,sans-serif;background:#1c2128;color:#e6e8eb;border-radius:10px;padding:12px 14px;width:320px;box-shadow:0 6px 24px rgba(0,0,0,.35);max-height:60vh;overflow:auto}
    b{color:#fff}.ok{color:#5bd28c}.warn{color:#f0b24a}button{all:unset;cursor:pointer;float:right;color:#8b94a1}
    a{color:#8fb4ff;cursor:pointer;display:block;margin:2px 0}</style><div class=p><button id=x>✕</button><div id=m>Reading the form…</div></div>`;
  document.documentElement.appendChild(host);
  const msg = sh.getElementById("m");
  sh.getElementById("x").onclick = () => { host.remove(); window.__jaRunning = false; };

  const top = window.top === window;
  if (top) await send({ type: "ctx", text: text(document.body?.innerText || "").slice(0, 8000) });
  else await send({ type: "ctx", text: text(document.body?.innerText || "").slice(0, 8000) });

  const hasCaptcha = !!document.querySelector("iframe[src*='recaptcha'], iframe[src*='hcaptcha'], iframe[src*='challenges.cloudflare'], .g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey]");

  if (!payloadFields.length) {
    if (!top) { host.remove(); window.__jaRunning = false; return; }  // frames without a form stay silent
    msg.innerHTML = "No empty form fields found here. If the form opens after a click (Apply / next step), open it and click the extension again.";
    window.__jaRunning = false;
    return;
  }

  const res = await send({ type: "fill", url: location.href, title: document.title, text: text(document.body?.innerText || ""), fields: payloadFields });
  if (res.error) {
    msg.innerHTML = `<b>Could not reach your agent.</b><br>Is the dashboard running (python -m jobagent serve)?<br><span class=warn>${res.error}</span>`;
    window.__jaRunning = false;
    return;
  }

  // ---- fill (React/Vue-safe: native setters + events)
  const fire = (e, ev) => e.dispatchEvent(new Event(ev, { bubbles: true }));
  function setText(e, v) {
    const proto = e.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(e, v);
    fire(e, "input"); fire(e, "change"); fire(e, "blur");
  }
  const mark = (e, color, tip) => { try { e.style.outline = `2px solid ${color}`; e.style.outlineOffset = "1px"; if (tip) e.title = tip; } catch (_) {} };
  const filled = [], left = [];
  for (const a of res.answers || []) {
    const f = els[a.id];
    if (!f) continue;
    const e = f.el, label = a.label || f.label || "(unlabelled field)";
    try {
      if (a.action === "fill") { e.focus(); setText(e, a.value); mark(e, "#1a8f4c", `Filled by Job Agent (${a.via})`); filled.push(label); }
      else if (a.action === "check") { if (!e.checked) e.click(); mark(e.parentElement || e, "#1a8f4c"); filled.push(label); }
      else if (a.action === "select" && f.kind === "select") { e.value = a.option_id; fire(e, "input"); fire(e, "change"); mark(e, "#1a8f4c", `Chose "${a.value}"`); filled.push(label); }
      else if (a.action === "select" && f.kind === "radio") {
        const r = f.items[+a.option_id]; (r.closest("label") || r).click(); if (!r.checked) r.click(); filled.push(label);
      }
      else if (a.action === "upload") {
        const r = await send({ type: "file", which: a.which, jid: a.jid });
        if (r.error || !r.b64) throw new Error("file unavailable");
        const bytes = Uint8Array.from(atob(r.b64), (c) => c.charCodeAt(0));
        const dt = new DataTransfer(); dt.items.add(new File([bytes], r.name, { type: "application/pdf" }));
        e.files = dt.files; fire(e, "input"); fire(e, "change"); filled.push(label || r.name);
      }
      else { mark(f.kind === "radio" ? f.items[0].closest("fieldset") || f.items[0] : e, "#f0b24a", a.reason || "left for you"); left.push({ label, reason: a.reason || "left for you", el: f.kind === "radio" ? f.items[0] : e }); }
    } catch (err) {
      left.push({ label, reason: "could not fill: " + err.message, el: e });
    }
  }
  // required fields the agent had no answer for were returned as "skip"; make sure anything still empty and required is listed
  const rn = res.resume;
  const resumeLine = rn ? (rn.ok ? `<div style="margin-top:6px">Resume tailored to this job (${esc2(rn.base)}).` +
      ((rn.added.length || rn.reworded.length) ? ` Skills adjusted: <span class=ok>${esc2([...rn.reworded, ...rn.added].join(", "))}</span>.` : " Your skills already matched; unchanged.") +
      (rn.not_added.length ? ` <span class=warn>Asked for but not in your resume, so not added: ${esc2(rn.not_added.join(", "))}.</span>` : "") + `</div>`
      : `<div class=warn style="margin-top:6px">Could not tailor the resume (${esc2(rn.error)}); uploaded your standard one.</div>`) : "";
  msg.innerHTML = `<b class=ok>Filled ${filled.length}</b> &nbsp;·&nbsp; <b class=warn>${left.length} left for you</b>` +
    (hasCaptcha ? `<div class=warn style="margin-top:6px">⚠ CAPTCHA on this page: solve it yourself.</div>` : "") +
    resumeLine + `<div style="margin-top:6px;color:#8b94a1">Check everything, then press the site's own Submit button.</div>`;
  for (const l of left) {
    const a = document.createElement("a");
    a.textContent = `• ${l.label.slice(0, 70)} — ${l.reason}`;
    a.onclick = () => { l.el.scrollIntoView({ behavior: "smooth", block: "center" }); };
    msg.appendChild(a);
  }
  await send({ type: "done", filled: filled.length, left: left.length });
  window.__jaRunning = false;
})();
