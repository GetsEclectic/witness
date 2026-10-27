// witness — meeting browser
//
// Routes (hash-based, no framework):
//   #/meetings or empty      → list of past meetings
//   #/meeting/<slug>         → transcript + summary + audio for one meeting
//   #actions                 → the recorder's tracked action items
//
// There is no live transcript: transcription runs in the post-meeting
// pipeline, so a meeting's text appears once that pipeline has run. The
// status bar is the only live thing here, and it polls /api/status.

const pane = document.getElementById("pane");
const statusEl = document.getElementById("status");
const statusLabel = statusEl.querySelector(".label");

function fmtClock(isoOrSec) {
  if (isoOrSec === null || isoOrSec === undefined) return "";
  if (typeof isoOrSec === "number") {
    const s = Math.floor(isoOrSec);
    const mm = Math.floor(s / 60).toString().padStart(2, "0");
    const ss = (s % 60).toString().padStart(2, "0");
    return `${mm}:${ss}`;
  }
  const d = new Date(isoOrSec);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function speakerLabel(evt) {
  // The two capture channels are the two speakers: ch0 is the local mic,
  // ch1 is everyone else on the call. Mirrors witness.render._speaker_label.
  if (evt.channel === "mic") return "You";
  if (evt.channel === "system") return "Remote";
  return "?";
}

function buildUtt(evt) {
  const row = document.createElement("div");
  row.className = `utt ${evt.channel}`;

  const who = document.createElement("div");
  who.className = "who";
  who.textContent = speakerLabel(evt);
  const ts = document.createElement("span");
  ts.className = "ts";
  ts.textContent = fmtClock(evt.ts_start);
  who.appendChild(ts);

  const what = document.createElement("div");
  what.className = "what";
  what.textContent = evt.text;

  row.appendChild(who);
  row.appendChild(what);
  return row;
}

// --- status bar ---

async function refreshStatus() {
  try {
    const resp = await fetch("/api/status");
    const s = await resp.json();
    statusEl.classList.toggle("recording", !!s.active);
    statusEl.classList.toggle("idle", !s.active);
    if (s.active) {
      statusLabel.innerHTML =
        `recording <span class="meta">${s.slug || ""}</span>`;
    } else {
      statusLabel.textContent = "idle";
    }
  } catch {
    statusLabel.textContent = "offline";
  }
}
setInterval(refreshStatus, 3000);

// --- Router ---

async function renderList() {
  pane.innerHTML = "<h1>past meetings</h1><p>loading…</p>";
  const resp = await fetch("/api/meetings");
  const meetings = await resp.json();
  if (!meetings.length) {
    pane.innerHTML = "<h1>past meetings</h1><p>(none yet)</p>";
    return;
  }
  const ul = document.createElement("ul");
  ul.className = "meeting-list";
  for (const m of meetings) {
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.href = `#/meeting/${encodeURIComponent(m.slug)}`;
    a.textContent = m.title || m.slug;
    li.appendChild(a);
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = [
      m.started_at ? new Date(m.started_at).toLocaleString() : "",
      m.duration_minutes != null ? `${m.duration_minutes}m` : "",
      m.has_summary ? "summary✓" : "",
      m.has_audio ? "audio✓" : "",
    ].filter(Boolean).join(" · ");
    li.appendChild(meta);
    if (m.tldr) {
      const tldr = document.createElement("div");
      tldr.className = "tldr";
      tldr.textContent = m.tldr;
      li.appendChild(tldr);
    }
    ul.appendChild(li);
  }
  pane.innerHTML = "<h1>past meetings</h1>";
  pane.appendChild(ul);
}

function summaryToHtml(md) {
  // Minimal markdown → HTML: headings, paragraphs, bullets.
  const lines = md.split("\n");
  const out = [];
  let inUl = false;
  for (const raw of lines) {
    const line = raw.trimEnd();
    if (line.startsWith("## ")) {
      if (inUl) { out.push("</ul>"); inUl = false; }
      out.push(`<h2>${escHtml(line.slice(3))}</h2>`);
    } else if (line.startsWith("# ")) {
      // skip — we already show title as <h1>
    } else if (line.startsWith("- ") || line.startsWith("* ")) {
      if (!inUl) { out.push("<ul>"); inUl = true; }
      out.push(`<li>${escHtml(line.slice(2))}</li>`);
    } else if (line.trim() === "") {
      if (inUl) { out.push("</ul>"); inUl = false; }
    } else {
      if (inUl) { out.push("</ul>"); inUl = false; }
      out.push(`<p>${escHtml(line)}</p>`);
    }
  }
  if (inUl) out.push("</ul>");
  return out.join("\n");
}

function escHtml(s) {
  return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

async function renderMeeting(slug) {
  pane.innerHTML = `<h1>loading…</h1>`;

  const [status, info, transcript, summaryRes] = await Promise.all([
    fetch("/api/status").then(r => r.ok ? r.json() : {}).catch(() => ({})),
    fetch(`/api/meetings/${slug}`).then(r => r.ok ? r.json() : {}),
    fetch(`/api/meetings/${slug}/transcript`).then(r => r.ok ? r.json() : []),
    fetch(`/api/meetings/${slug}/summary`).then(r => r.ok ? r.json() : null),
  ]);
  const summaryMd = summaryRes?.markdown ?? null;
  const title = info?.title || slug;
  // The transcript for a meeting still in progress is whatever the last
  // pipeline run produced — nothing at all until the first pause. Say so
  // rather than showing an empty pane that looks like a failure.
  const live = status.active && status.slug === slug;
  const note = live
    ? `<p class="muted">recording now — the transcript below is from the last completed segment, and fills in when the meeting ends.</p>`
    : "";
  pane.innerHTML = `
    <h1>${escHtml(title)}</h1>
    ${note}
    ${summaryMd ? `<details class="summary" open><summary>summary</summary><div class="summary-body">${summaryToHtml(summaryMd)}</div></details>` : ""}
    ${info?.has_audio ? `<p><audio controls src="/api/meetings/${slug}/audio" style="width:100%;max-width:40rem;"></audio></p>` : ""}
    ${info?.action_items?.length ? `<p class="muted">${info.action_items.length} action item(s) · <a href="#actions">tracked items</a></p>` : ""}
    <div id="utts"></div>
  `;
  const utts = pane.querySelector("#utts");
  for (const evt of transcript) {
    utts.appendChild(buildUtt(evt));
  }
}

// --- action items ---

const ACTION_GROUPS = [
  ["needs_input", "needs your input"],
  ["drafted", "drafted"],
  ["open", "open"],
];

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = text;
  return n;
}

async function setActionStatus(id, status) {
  const resp = await fetch(`/api/actions/${encodeURIComponent(id)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
  if (!resp.ok) alert(`could not mark ${id} ${status}: ${resp.status}`);
  renderActions();
}

// Full Slack and Docs URLs are noise; name the thing instead.
function linkLabel(href) {
  let u;
  try { u = new URL(href, location.href); } catch { return href; }
  if (u.hostname.endsWith("slack.com")) {
    return /\/p\d+/.test(u.pathname) ? "Slack message" : "Slack conversation";
  }
  if (u.hostname === "docs.google.com") {
    const kind = u.pathname.split("/")[1];
    return { document: "Google Doc", spreadsheets: "Google Sheet", presentation: "Google Slides" }[kind] || "Google file";
  }
  if (/gitlab|github/.test(u.hostname)) {
    const mr = u.pathname.match(/merge_requests\/(\d+)|pull\/(\d+)/);
    if (mr) return `${u.pathname.split("/-/")[0].split("/").pop()} !${mr[1] || mr[2]}`;
  }
  return u.hostname + u.pathname;
}

function buildArtifact(link) {
  const li = el("li");
  if (link.href) {
    // A served file reads better as its path inside the meeting folder.
    const served = link.href.match(/^\/api\/meetings\/[^/]+\/files\/(.+)$/);
    const a = el("a", null, served ? decodeURIComponent(served[1]) : linkLabel(link.href));
    a.title = link.text;
    a.href = link.href;
    a.target = "_blank";
    a.rel = "noopener";
    li.appendChild(a);
    return li;
  }
  // A path the app can't serve: show it, and copy it on click.
  const code = el("code", "copyable", link.text);
  code.title = "click to copy";
  code.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(link.text);
      code.classList.add("copied");
      setTimeout(() => code.classList.remove("copied"), 1200);
    } catch {}
  });
  li.appendChild(code);
  return li;
}

function buildActionCard(item) {
  const card = el("article", `action ${item.status}`);
  card.id = `action-${item.id}`;

  const head = el("div", "action-head");
  head.appendChild(el("span", `badge ${item.status}`, item.status.replace("_", " ")));
  head.appendChild(document.createTextNode(" "));
  head.appendChild(el("span", "action-text", item.text));
  card.appendChild(head);

  if (item.note) {
    const note = el("div", "action-note", item.note);
    card.appendChild(note);
  }

  const meta = el("div", "action-meta");
  if (item.meeting_exists) {
    const a = el("a", null, item.meeting_title);
    a.href = `#/meeting/${encodeURIComponent(item.slug)}`;
    meta.appendChild(a);
  } else {
    meta.appendChild(el("span", null, item.meeting_title));
  }
  const bits = [
    item.transcript_ts ? `[${item.transcript_ts}]` : "",
    item.owner,
    item.people?.length ? `with ${item.people.join(", ")}` : "",
    item.due ? `due ${item.due}` : "",
    item.kind ? item.kind.replace("_", " ") : "",
  ].filter(Boolean);
  for (const b of bits) {
    meta.appendChild(el("span", "sep", " · "));
    meta.appendChild(el("span", null, b));
  }
  card.appendChild(meta);

  if (item.links?.length) {
    const ul = el("ul", "action-artifacts");
    for (const link of item.links) ul.appendChild(buildArtifact(link));
    card.appendChild(ul);
  }

  const buttons = el("div", "action-buttons");
  const closed = item.status === "done" || item.status === "dropped";
  const choices = closed ? [["open", "reopen"]] : [["done", "done"], ["dropped", "drop"]];
  for (const [status, label] of choices) {
    const btn = el("button", status, label);
    btn.addEventListener("click", () => setActionStatus(item.id, status));
    buttons.appendChild(btn);
  }
  card.appendChild(buttons);
  return card;
}

function updateActionsNav(items) {
  const n = items.filter(i => !["done", "dropped"].includes(i.status)).length;
  document.getElementById("nav-actions").textContent =
    n ? `action items (${n})` : "action items";
}

async function renderActions() {
  const closedWasOpen = !!pane.querySelector("details.closed-actions[open]");
  const items = await fetch("/api/actions").then(r => r.ok ? r.json() : []);
  updateActionsNav(items);
  pane.innerHTML = "<h1>action items</h1>";
  if (!items.length) {
    pane.appendChild(el("p", "muted",
      "(none tracked yet: items appear here once a meeting's summary names you as an owner)"));
    return;
  }
  for (const [status, label] of ACTION_GROUPS) {
    const group = items.filter(i => i.status === status);
    if (!group.length) continue;
    const section = el("section", "action-group");
    section.appendChild(el("h2", null, `${label} · ${group.length}`));
    for (const item of group) section.appendChild(buildActionCard(item));
    pane.appendChild(section);
  }
  const closed = items.filter(i => i.status === "done" || i.status === "dropped");
  if (closed.length === items.length) pane.appendChild(el("p", "muted", "nothing open"));
  if (closed.length) {
    const details = el("details", "closed-actions");
    details.open = closedWasOpen;
    details.appendChild(el("summary", null, `done / dropped · ${closed.length}`));
    for (const item of closed) details.appendChild(buildActionCard(item));
    pane.appendChild(details);
  }
}

function route() {
  const h = location.hash;
  if (h === "#actions" || h === "#/actions") return renderActions();
  const m = h.match(/^#\/meeting\/(.+)$/);
  if (m) return renderMeeting(decodeURIComponent(m[1]));
  return renderList();
}
window.addEventListener("hashchange", route);

refreshStatus();
route();
fetch("/api/actions").then(r => r.ok ? r.json() : []).then(updateActionsNav).catch(() => {});
