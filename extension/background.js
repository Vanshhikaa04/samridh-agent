// Click the toolbar icon -> run the filler in every frame of this tab. All calls to your local agent happen here
// (the service worker is allowed to reach 127.0.0.1; web pages are not, and never see the secret token).
importScripts("config.js");

const ctxByTab = {};  // longest page text seen per tab (job text lives in the top frame, forms are often in an iframe)

async function api(path, init = {}) {
  const r = await fetch(JA_BASE + path, { ...init, headers: { "X-Agent-Token": JA_TOKEN, "Content-Type": "application/json", ...(init.headers || {}) } });
  if (!r.ok) throw new Error(`agent said ${r.status}`);
  return r;
}

chrome.action.onClicked.addListener(async (tab) => {
  if (!tab.id || !/^https?:/.test(tab.url || "")) return;
  ctxByTab[tab.id] = "";
  chrome.action.setBadgeBackgroundColor({ tabId: tab.id, color: "#2f6fed" });
  chrome.action.setBadgeText({ tabId: tab.id, text: "…" });
  try {
    await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, files: ["content.js"] });
  } catch (e) {
    chrome.action.setBadgeText({ tabId: tab.id, text: "!" });
  }
});

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  const tabId = sender.tab && sender.tab.id;
  (async () => {
    if (msg.type === "ctx") {
      if ((msg.text || "").length > (ctxByTab[tabId] || "").length) ctxByTab[tabId] = msg.text;
      return { ok: true };
    }
    if (msg.type === "fill") {
      const body = { url: msg.url, title: msg.title, page_text: (ctxByTab[tabId] || msg.text || "").slice(0, 8000), fields: msg.fields };
      const r = await api("/api/extension/fill", { method: "POST", body: JSON.stringify(body) });
      return await r.json();
    }
    if (msg.type === "file") {
      const r = await api("/api/extension/file/" + msg.which + (msg.jid ? "?jid=" + encodeURIComponent(msg.jid) : ""));
      const buf = new Uint8Array(await r.arrayBuffer());
      let bin = "";
      for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
      return { b64: btoa(bin), name: r.headers.get("X-Filename") || (msg.which + ".pdf") };
    }
    if (msg.type === "done" && tabId) {
      chrome.action.setBadgeBackgroundColor({ tabId, color: msg.left ? "#d98c00" : "#1a8f4c" });
      chrome.action.setBadgeText({ tabId, text: String(msg.filled) });
      return { ok: true };
    }
    return { ok: false };
  })().then(reply).catch((e) => reply({ error: String(e.message || e) }));
  return true;  // async reply
});
