"""Local tracker UI: python -m jobagent serve  ->  http://localhost:8765"""
import json
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import db
from .config import env

_APPLY_LOCK = threading.Lock()  # one browser apply at a time from the dashboard

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Job Tracker</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1c2128;--mut:#6b7280;--line:#e3e6ea;--acc:#2f6fed}
@media(prefers-color-scheme:dark){:root{--bg:#14171c;--card:#1c2128;--fg:#e6e8eb;--mut:#8b94a1;--line:#2b323b;--acc:#6b9bff}}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
header{padding:16px 20px;display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between}
h1{font-size:18px;margin:0}.stats{display:flex;gap:8px;flex-wrap:wrap}
.chip{padding:4px 10px;border:1px solid var(--line);border-radius:99px;background:var(--card);cursor:pointer;color:var(--mut)}
.chip.on{border-color:var(--acc);color:var(--acc);font-weight:600}
main{padding:0 20px 40px;overflow-x:auto}
form.add{display:flex;gap:8px;align-items:center;padding:0 20px 14px;flex-wrap:wrap}form.add input{flex:1;min-width:260px}
button{font:inherit;color:#fff;background:var(--acc);border:0;border-radius:6px;padding:6px 14px;cursor:pointer}button:disabled{opacity:.5}
.s-applying{border-left:3px solid #8a5cf6}.s-needs_input{border-left:3px solid #e07b39}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden}
th,td{padding:9px 12px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
a{color:var(--acc);text-decoration:none}.mut{color:var(--mut);font-size:12px}
select,input{font:inherit;color:inherit;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:5px 7px}
input.notes{width:100%;min-width:180px}.score{font-weight:600}
.s-applied{border-left:3px solid #2f6fed}.s-interview{border-left:3px solid #d99a06}.s-offer{border-left:3px solid #1a9c55}
.s-rejected{border-left:3px solid #d64545}.s-ghosted{border-left:3px solid #8b94a1}
.saved{color:#1a9c55;font-size:12px;opacity:0;transition:opacity .3s}.saved.show{opacity:1}
</style>
<header><h1>Job Tracker <a href="/outreach" style="font-size:13px;font-weight:400;margin-left:12px">Outreach →</a> <a href="/inbox" style="font-size:13px;font-weight:400;margin-left:8px">Inbox →</a> <a href="/logins" style="font-size:13px;font-weight:400;margin-left:8px">Logins →</a> %NAV%</h1><div class=stats id=chips></div></header>
<div class=mut id=sum style="padding:0 20px 8px"></div>
<form class=add onsubmit="addLink(event)"><input id=link type=url required placeholder="Paste a job link to apply to it..."><button id=addbtn>Apply to link</button><span id=addmsg class=mut></span></form>
<main><table><thead><tr><th>Score<th>Role<th>Location<th>Posted<th>Applicants<th>Status<th>Notes<th>Resume</thead><tbody id=rows></tbody></table></main>
<script>
const ST=%STATUSES%;let jobs=[],filter='active';
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function load(){jobs=await (await fetch('%API%')).json();draw()}
function shown(){return jobs.filter(j=>filter==='all'||(filter==='active'?!['filtered','skipped'].includes(j.status):j.status===filter))}
function draw(){
  const cnt=s=>jobs.filter(j=>j.status===s).length;
  const today=new Date().toISOString().slice(0,10);
  sum.innerHTML=`<b>%LABEL%</b> &nbsp; applied today <b>${jobs.filter(j=>j.status==='applied'&&(j.applied_at||'').slice(0,10)===today).length}</b> · applied total <b>${cnt('applied')+cnt('interview')+cnt('offer')+cnt('rejected')+cnt('ghosted')}</b> · ready <b>${cnt('ready')}</b> · needs input <b>${cnt('needs_input')}</b> · interviews <b>${cnt('interview')}</b> · rejected <b>${cnt('rejected')}</b> · offers <b>${cnt('offer')}</b>`;
  chips.innerHTML=['active','all',...ST].map(s=>`<span class="chip ${s===filter?'on':''}" onclick="filter='${s}';draw()">${s}${ST.includes(s)?' '+cnt(s):''}</span>`).join('');
  rows.innerHTML=shown().map((j,i)=>`<tr class="s-${j.status}"><td class=score>${j.score??'-'}
   <td><a href="${esc(j.url)}" target=_blank>${esc(j.title)}</a><div class=mut>${esc(j.company)} · ${esc(j.source)}${j.applied_at?' · applied '+esc(j.applied_at.slice(0,10)):''}</div><div class=mut>${esc(j.reason)}</div>
   <td>${esc(j.location)}
   <td class=mut>${esc((j.posted_at||'').slice(0,10))}
   <td class=mut>${j.applicants==null?'-':esc(j.applicants)}
   <td><select onchange="save(${jobs.indexOf(j)},this.value,null,this)">${ST.map(s=>`<option ${s===j.status?'selected':''}>${s}</option>`).join('')}</select> <span class=saved></span>
   <td><input class=notes value="${esc(j.notes)}" placeholder="recruiter, dates, next step..." onchange="save(${jobs.indexOf(j)},null,this.value,this)">
   <td>${resumeCell(j)}`).join('')||'<tr><td colspan=8 class=mut>Nothing here.'}
async function save(i,status,notes,el){
  const j=jobs[i];if(status!==null)j.status=status;if(notes!==null)j.notes=notes;
  const r=await fetch('/api/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:j.id,status,notes})});
  if(!r.ok){alert('Save failed');return}
  const u=await r.json();j.applied_at=u.applied_at;
  if(status!==null){draw()}else{const s=el.closest('tr').querySelector('.saved');s.textContent='saved';s.classList.add('show');setTimeout(()=>s.classList.remove('show'),1200)}
}
function resumeCell(j){
  if(j.status==='filtered')return '';
  const id=encodeURIComponent(j.id);
  return j.has_resume?`<a href="/resume-view?id=${id}" target=_blank>View resume</a>`
                     :`<button onclick="tailorJob(${jobs.indexOf(j)},this)">Tailor</button>`;
}
async function tailorJob(i,btn){
  btn.disabled=true;btn.textContent='Tailoring...';
  const r=await fetch('/api/resume/tailor',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:jobs[i].id})});
  if(r.ok){window.open('/resume-view?id='+encodeURIComponent(jobs[i].id),'_blank');await load()}
  else{btn.disabled=false;btn.textContent='Tailor';alert('Could not tailor: '+(await r.text()))}
}
async function addLink(e){
  e.preventDefault();addbtn.disabled=true;addmsg.textContent='Added - applying in the background...';
  const r=await fetch('/api/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:link.value})});
  addbtn.disabled=false;
  if(r.ok){link.value='';filter='all';await load()}else{addmsg.textContent='Could not add that link'}
}
load();
// keep statuses live (the agent updates the DB in the background); don't interrupt typing
setInterval(()=>{const a=document.activeElement;if(!a||!['INPUT','SELECT'].includes(a.tagName))load()},15000);
</script>"""


OUTREACH_PAGE = (Path(__file__).parent / "outreach.html").read_text(encoding="utf-8")
INBOX_PAGE = (Path(__file__).parent / "inbox.html").read_text(encoding="utf-8")
LOGINS_PAGE = (Path(__file__).parent / "logins.html").read_text(encoding="utf-8")


def _tracker_page(kind: str) -> str:
    """The same tracker for jobs or for internships (same chips, counts and table)."""
    intern = kind == "internship"
    nav = '<a href="/" style="font-size:13px;font-weight:400;margin-left:8px">Jobs →</a>' if intern else '<a href="/internships" style="font-size:13px;font-weight:400;margin-left:8px">Internships →</a>'
    html = PAGE.replace("%STATUSES%", json.dumps(db.STATUSES)).replace("%NAV%", nav).replace("%API%", "/api/jobs?kind=" + kind)
    html = html.replace("%LABEL%", "Internships" if intern else "Jobs")
    return html.replace("<title>Job Tracker</title>", "<title>Internship Tracker</title>").replace("<h1>Job Tracker", "<h1>Internship Tracker") if intern else html


def _resume_page(jid: str) -> str:
    """Viewer: the tailored PDF next to a plain-English list of exactly what was changed (and what was refused)."""
    from html import escape as e
    from urllib.parse import quote
    c = db.conn()
    job = c.execute("SELECT title, company FROM jobs WHERE id=?", (jid,)).fetchone()
    row = c.execute("SELECT report, created_at FROM resumes WHERE job_id=?", (jid,)).fetchone()
    if not job or not row:
        return "<p style='font:16px system-ui;padding:24px'>No tailored resume for this job yet. Press <b>Tailor</b> on the dashboard.</p>"
    rep = json.loads(row["report"])
    parts = []
    for label, r in rep["rows"].items():
        tags = []
        for z in r["reworded"]:
            tags.append(f"<li><b>Reworded</b> {e(z['from'])} &rarr; <b>{e(z['to'])}</b> <span class=mut>(same thing, the job's wording)</span></li>")
        for a in r["added"]:
            tags.append(f"<li><b>Added</b> {e(a)} <span class=mut>(already in your resume's own text)</span></li>")
        for x in r["removed"]:
            tags.append(f"<li><b>Made room by removing</b> {e(x)} <span class=mut>(least relevant to this job)</span></li>")
        if r["reordered"]:
            tags.append("<li><b>Reordered</b> so the skills this job asks for come first</li>")
        body = "".join(tags) or "<li class=mut>unchanged</li>"
        parts.append(f"<h3>{e(label)}</h3><ul>{body}</ul><div class=mut>{e(', '.join(r['after']))}</div>")
    miss = rep.get("not_added") or []
    miss_html = ("<div class=warn><b>Not added: the job asks for these, but nothing in your resume shows them</b><div>" + e(", ".join(miss)) +
                 "</div><div class=mut>If you truly have one of them, add it under <code>known_skills</code> in profile.yaml and press Regenerate. "
                 "The agent will never add a skill it can't back up.</div></div>") if miss else "<div class=mut>Every technical requirement is already covered by your resume.</div>"
    q = quote(jid, safe="")
    return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Resume for this job</title>
<style>:root{{--bg:#f6f7f9;--card:#fff;--fg:#1c2128;--mut:#6b7280;--line:#e3e6ea;--acc:#2f6fed}}
@media(prefers-color-scheme:dark){{:root{{--bg:#14171c;--card:#1c2128;--fg:#e6e8eb;--mut:#8b94a1;--line:#2b323b;--acc:#6b9bff}}}}
*{{box-sizing:border-box}}body{{margin:0;font:14px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--fg)}}a{{color:var(--acc);text-decoration:none}}
header{{padding:14px 20px;display:flex;flex-wrap:wrap;gap:14px;align-items:center;justify-content:space-between}}h1{{font-size:17px;margin:0}}
.wrap{{display:flex;gap:16px;padding:0 20px 24px;flex-wrap:wrap}}iframe{{flex:1 1 560px;min-width:320px;height:86vh;border:1px solid var(--line);border-radius:8px;background:#fff}}
aside{{flex:0 1 380px;min-width:280px;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 16px 16px}}h3{{font-size:13px;margin:14px 0 4px}}
ul{{margin:0 0 4px;padding-left:18px}}.mut{{color:var(--mut);font-size:12px}}.warn{{margin-top:16px;padding:10px 12px;border-radius:8px;border:1px solid #d99a06;background:rgba(217,154,6,.1)}}
button{{font:inherit;color:#fff;background:var(--acc);border:0;border-radius:6px;padding:5px 12px;cursor:pointer}}code{{background:var(--bg);padding:1px 5px;border-radius:4px}}</style>
<header><h1>Resume for: {e(job['title'])} @ {e(job['company'])}</h1>
<div><a href="/resume/{q}" target=_blank>Open PDF</a> &middot; <a href="/resume/__original__" target=_blank>Your original resume</a> &middot; <a href="/">&larr; Dashboard</a> &nbsp;
<button onclick="regen()">Regenerate</button></div></header>
<div class=wrap><iframe src="/resume/{q}#toolbar=0&navpanes=0&view=FitH"></iframe>
<aside><h2 style="font-size:15px;margin:12px 0 0">What changed (technical skills only)</h2><div class=mut>Everything else on the page is exactly your original resume.</div>
{''.join(parts)}{miss_html}<div class=mut style="margin-top:12px">Made {e(row['created_at'])}</div></aside></div>
<script>async function regen(){{const b=event.target;b.disabled=true;b.textContent='Working...';
const r=await fetch('/api/resume/tailor',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{id:{json.dumps(jid)},force:true}})}});
if(r.ok)location.reload();else{{alert('Failed: '+await r.text());b.disabled=false;b.textContent='Regenerate'}}}}</script>"""


class H(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/extension/file/"):  # resume / cover letter bytes for the browser extension (token required)
            from . import extension_api
            if not extension_api.authorized(self.headers):
                return self._send(403, b"forbidden", "text/plain")
            from urllib.parse import parse_qs as _pq, urlparse as _up
            _u = _up(self.path)
            got = extension_api.file_bytes(_u.path.rsplit("/", 1)[-1], _pq(_u.query).get("jid", [""])[0])
            if not got:
                return self._send(404, b"no such file", "text/plain")
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(got[0])))
            self.send_header("X-Filename", got[1])
            self.end_headers()
            return self.wfile.write(got[0])
        if self.path.startswith("/resume-view"):
            from urllib.parse import parse_qs, urlparse
            jid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            return self._send(200, _resume_page(jid).encode(), "text/html; charset=utf-8")
        if self.path.startswith("/resume/"):
            from urllib.parse import unquote
            from . import tailor
            jid = unquote(self.path[len("/resume/"):].split("#")[0])
            row = db.conn().execute("SELECT path FROM resumes WHERE job_id=?", (jid,)).fetchone()
            path = tailor.base_resume() if jid == "__original__" else (Path(row["path"]) if row else None)
            if not path or not Path(path).exists():
                return self._send(404, b"no tailored resume for this job yet", "text/plain")
            return self._send(200, Path(path).read_bytes(), "application/pdf")
        if self.path == "/logins":
            return self._send(200, LOGINS_PAGE.encode(), "text/html; charset=utf-8")
        if self.path == "/api/logins":
            from . import sitelogin
            return self._send(200, json.dumps(sitelogin.status()).encode(), "application/json")
        if self.path == "/inbox":
            return self._send(200, INBOX_PAGE.encode(), "text/html; charset=utf-8")
        if self.path == "/api/inbox":
            since = env("DASHBOARD_SINCE", "")  # only show this account's own activity, not records carried over in the shared database
            rows = db.conn().execute("SELECT * FROM inbox WHERE received_at >= ? ORDER BY (kind='rejection') DESC, (kind='interview') DESC, received_at DESC LIMIT 500", (since,)).fetchall()
            return self._send(200, json.dumps([dict(r) for r in rows]).encode(), "application/json")
        if self.path == "/outreach":
            return self._send(200, OUTREACH_PAGE.encode(), "text/html; charset=utf-8")
        if self.path.split("?")[0] == "/api/followups":
            from urllib.parse import parse_qs, urlparse
            from . import outreach
            days = max(0, int((parse_qs(urlparse(self.path).query).get("days") or ["5"])[0]))
            c = db.conn()
            rows = [{k: r[k] for k in ("id", "company", "display_company", "name", "first_name", "title", "email", "subject", "sent_at", "fcount", "flast")}
                    for r in outreach.followup_candidates(c, days)]
            cap = int(__import__("os").environ.get("OUTREACH_MAX_PER_DAY", "15"))
            meta = {"smtp": outreach.smtp_ready(), "cap": cap, "used": outreach.sent_today(c) + outreach.followups_today(c), "default": outreach.DEFAULT_FOLLOWUP}
            return self._send(200, json.dumps({"meta": meta, "rows": rows}).encode(), "application/json")
        if self.path == "/api/outreach":
            from . import outreach
            c = db.conn()
            since = env("DASHBOARD_SINCE", "")
            rows = c.execute("SELECT * FROM outreach WHERE coalesce(sent_at, created_at, '') >= ? ORDER BY (status='drafted') DESC, (status='sent') DESC, created_at DESC, id DESC", (since,)).fetchall()
            meta = {"smtp": outreach.smtp_ready(), "auto": __import__("os").environ.get("OUTREACH_AUTO_SEND", "0") == "1",
                    "sent_today": outreach.sent_today(c), "cap": int(__import__("os").environ.get("OUTREACH_MAX_PER_DAY", "15"))}
            return self._send(200, json.dumps({"meta": meta, "rows": [dict(r) for r in rows]}).encode(), "application/json")
        if self.path == "/":
            self._send(200, _tracker_page("job").encode(), "text/html; charset=utf-8")
        elif self.path == "/internships":
            self._send(200, _tracker_page("internship").encode(), "text/html; charset=utf-8")
        elif self.path.split("?")[0] == "/api/jobs":
            rows = db.conn().execute(
                "SELECT id,source,title,company,location,url,status,score,reason,notes,applied_at,posted_at,applicants,updated_at,found_at, "
                "(SELECT 1 FROM resumes r WHERE r.job_id=jobs.id) AS has_resume "
                "FROM jobs ORDER BY (status IN ('filtered','skipped')), score IS NULL, score DESC"
            ).fetchall()
            from urllib.parse import parse_qs, urlparse
            from .filters import is_internship
            since = env("DASHBOARD_SINCE", "")
            if since:  # drop applications/outcomes recorded before this account started; keep the open job pool
                handled = ("applied", "interview", "offer", "rejected", "ghosted", "needs_input", "applying", "draft_email", "dry_run_ok", "failed")
                rows = [r for r in rows if (r["found_at"] or "") >= since or (r["applied_at"] or "") >= since
                        or (r["status"] in handled and (r["updated_at"] or "") >= since)]  # only jobs this account found or acted on
            kind = parse_qs(urlparse(self.path).query).get("kind", [""])[0]
            if kind in ("job", "internship"):
                rows = [r for r in rows if is_internship(r["title"]) == (kind == "internship")]
            self._send(200, json.dumps([dict(r) for r in rows]).encode(), "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/api/extension/fill":  # the browser extension: answers for the form you have open
            from . import extension_api
            if not extension_api.authorized(self.headers):
                return self._send(403, b"forbidden", "text/plain")
            try:
                return self._send(200, json.dumps(extension_api.fill(d)).encode(), "application/json")
            except Exception as e:
                return self._send(500, f"{type(e).__name__}: {e}".encode(), "text/plain")
        # Any other POST must come from this dashboard itself: a web page you happen to visit must not be able to
        # make the agent apply, e-mail people or edit your list. Browsers always send Origin on cross-site POSTs.
        origin = self.headers.get("Origin", "")
        if origin and urlparse(origin).hostname not in ("localhost", "127.0.0.1"):
            return self._send(403, b"forbidden origin", "text/plain")
        if self.path in ("/api/login/start", "/api/login/save"):
            from . import sitelogin
            fn = sitelogin.start_login if self.path.endswith("start") else sitelogin.save_login
            return self._send(200, json.dumps(fn(d.get("site", ""))).encode(), "application/json")
        if self.path == "/api/add":
            url = (d.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                return self._send(400, b"bad url", "text/plain")
            from .daemon import apply_one
            from .manual import add_link
            from .config import env
            jid = add_link(url)
            live = env("MANUAL_LINK_SUBMIT", "1") == "1"
            def work():
                with _APPLY_LOCK:
                    apply_one(jid, live)
            threading.Thread(target=work, daemon=True).start()
            return self._send(200, json.dumps({"id": jid}).encode(), "application/json")
        if self.path == "/api/resume/tailor":
            from . import tailor
            row = db.conn().execute("SELECT * FROM jobs WHERE id=?", (d.get("id", ""),)).fetchone()
            if not row:
                return self._send(404, b"unknown job", "text/plain")
            try:
                tailor.tailor(dict(row), force=bool(d.get("force")))
            except Exception as e:
                return self._send(500, f"{type(e).__name__}: {e}".encode(), "text/plain")
            return self._send(200, b'{"ok":true}', "application/json")
        if self.path == "/api/followups/send":  # follow-ups: your message laid over each selected recipient
            from . import outreach
            c = db.conn()
            ids = [int(x) for x in d.get("ids", []) if str(x).isdigit()]
            if not ids:
                return self._send(400, json.dumps({"ok": False, "message": "Select at least one email."}).encode(), "application/json")
            if not outreach.smtp_ready():
                return self._send(400, json.dumps({"ok": False, "message": "Sending is off (no Gmail password in .env)."}).encode(), "application/json")
            cap = int(__import__("os").environ.get("OUTREACH_MAX_PER_DAY", "15"))
            room = max(0, cap - outreach.sent_today(c) - outreach.followups_today(c))
            note = f"Sending {min(len(ids), room)} follow-up(s), 20-45 seconds apart."
            if len(ids) > room:
                note = f"Daily cap of {cap} leaves room for {room}: sending those, the rest can go tomorrow."
            threading.Thread(target=outreach.send_followups, args=(ids[:room], d.get("message", ""), bool(d.get("attach"))), daemon=True).start()
            return self._send(200, json.dumps({"ok": True, "note": note, "queued": min(len(ids), room)}).encode(), "application/json")
        if self.path == "/api/followups/preview":
            from . import outreach
            return self._send(200, json.dumps({"text": outreach.superimpose(d.get("message", ""), d.get("first_name", ""), d.get("company", "Acme"))}).encode(), "application/json")
        if self.path == "/api/outreach/add":  # people typed into the Outreach page
            from . import outreach
            entries = outreach.parse_entries(d.get("text", ""))
            if not entries:
                return self._send(400, json.dumps({"ok": False, "message": "Type at least one email address."}).encode(), "application/json")
            c = db.conn()
            results = outreach.add_manual(c, entries)
            ids = [r["id"] for r in results if r.get("ok")]
            send, note = bool(d.get("send")), ""
            if send and ids:
                if not outreach.smtp_ready():
                    note = "Sending is off (no Gmail password in .env): added as drafts instead."
                else:
                    cap = int(__import__("os").environ.get("OUTREACH_MAX_PER_DAY", "15"))
                    room = max(0, cap - outreach.sent_today(c))
                    if len(ids) > room:
                        note = f"Daily cap of {cap} leaves room for {room}: the rest stay as drafts."
                    threading.Thread(target=outreach.send_manual, args=(ids[:room],), daemon=True).start()
                    note = note or f"Sending {len(ids)} email(s), about {15 if len(ids) > 1 else 0}-35 seconds apart."
            elif ids:
                note = "Added as drafts: review them below and press Send."
            return self._send(200, json.dumps({"ok": True, "results": results, "note": note}).encode(), "application/json")
        if self.path.startswith("/api/outreach/"):
            from . import outreach
            c = db.conn()
            oid, action = int(d.get("id", 0)), self.path.rsplit("/", 1)[-1]
            if action == "send":  # send in a thread: SMTP can take a few seconds
                ok, why = outreach.send_row(c, oid)
                return self._send(200 if ok else 400, json.dumps({"ok": ok, "message": why}).encode(), "application/json")
            if action == "skip":
                c.execute("UPDATE outreach SET status='skipped' WHERE id=? AND status IN ('drafted','failed')", (oid,))
            elif action == "restore":
                c.execute("UPDATE outreach SET status='drafted' WHERE id=? AND status IN ('skipped','failed')", (oid,))
            elif action == "status" and d.get("status") in ("replied", "bounced", "sent", "drafted", "skipped"):
                c.execute("UPDATE outreach SET status=?, replied_at=CASE WHEN ?='replied' THEN datetime('now') ELSE replied_at END WHERE id=?",
                          (d["status"], d["status"], oid))
            elif action == "update":
                if d.get("email") is not None and "@" in d["email"]:
                    c.execute("UPDATE outreach SET email=?, source='edited by you', confidence='high', "
                              "status=CASE WHEN status='needs_email' THEN 'drafted' ELSE status END "
                              "WHERE id=? AND status IN ('drafted','failed','needs_email')", (d["email"].strip().lower(), oid))
                if d.get("first_name") is not None:
                    c.execute("UPDATE outreach SET first_name=? WHERE id=? AND status IN ('drafted','failed','needs_email')", (d["first_name"].strip(), oid))
                c.commit()
                outreach.regenerate(c, oid)
            c.commit()
            return self._send(200, b'{"ok":true}', "application/json")
        if self.path != "/api/update":
            return self._send(404, b"not found", "text/plain")
        c = db.conn()
        if d.get("status") is not None:
            if d["status"] not in db.STATUSES:
                return self._send(400, b"bad status", "text/plain")
            c.execute("UPDATE jobs SET status=?, updated_at=datetime('now') WHERE id=?", (d["status"], d["id"]))
            if d["status"] == "applied":
                c.execute("UPDATE jobs SET applied_at=COALESCE(applied_at, datetime('now')) WHERE id=?", (d["id"],))
        if d.get("notes") is not None:
            c.execute("UPDATE jobs SET notes=?, updated_at=datetime('now') WHERE id=?", (d["notes"], d["id"]))
        c.commit()
        row = c.execute("SELECT applied_at FROM jobs WHERE id=?", (d["id"],)).fetchone()
        self._send(200, json.dumps({"applied_at": row["applied_at"] if row else None}).encode(), "application/json")

    def log_message(self, *a):
        pass


def serve(port: int = 8765):
    from . import extension_api
    extension_api.ensure_token()  # writes extension/config.js with the secret the extension needs
    print(f"Tracker running at http://localhost:{port}  (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
