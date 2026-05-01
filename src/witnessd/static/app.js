// witness — live UI
//
// Routes (hash-based, no framework):
//   #/live  or empty         → live transcript pane, WebSocket to /ws
//   #/meetings               → list of past meetings
//   #/meeting/<slug>         → static transcript + audio for one meeting
//   #/unknowns               → identify unbound voiceprints (audio + bind)

const pane = document.getElementById("pane");
const statusEl = document.getElementById("status");
const statusLabel = statusEl.querySelector(".label");
const jumpBtn = document.getElementById("jump-live");

let ws = null;
let wsBackoff = 500;
let autoScroll = true;
// Server sends *something* (event or ping) at least every 30s. If we go
// noticeably longer without hearing anything, the socket is half-open
// (laptop slept, network blipped) — force-close to trigger reconnect,
// since the browser's own close detection can take many minutes.
let lastMsgAt = 0;
let livenessTimer = null;
const LIVENESS_TIMEOUT_MS = 45000;
// One "in-progress" interim DOM node per channel. When a final event lands,
// we finalize the node (strip interim styling) and clear the slot.
const interimSlot = { mic: null, system: null };

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
  // Mic channel is always the local user (post-AEC, no diarization there).
  if (evt.channel === "mic") return "You";
  const sp = evt.speaker || "";
  if (sp.startsWith("system_speaker_")) return "Remote " + sp.slice("system_speaker_".length);
  if (sp.startsWith("mic_speaker_")) return "You";
  if (sp.startsWith("speaker_")) return "Spk " + sp.slice("speaker_".length);  // legacy
  return "Remote";
}

function buildUtt(evt) {
  const row = document.createElement("div");
  row.className = `utt ${evt.channel}${evt.is_final ? "" : " interim"}`;

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

function handleEvent(evt) {
  // If user scrolled up, don't yank them back.
  const atBottom = Math.abs(
    pane.scrollHeight - pane.scrollTop - pane.clientHeight
  ) < 40;
  autoScroll = atBottom;
  jumpBtn.hidden = autoScroll;

  const slot = interimSlot[evt.channel];

  if (!evt.is_final) {
    // Update or create the channel's running interim.
    if (slot) {
      slot.querySelector(".what").textContent = evt.text;
      slot.querySelector(".ts").textContent = fmtClock(evt.ts_start);
    } else {
      const row = buildUtt(evt);
      pane.appendChild(row);
      interimSlot[evt.channel] = row;
    }
  } else {
    // Final. Replace the interim slot with this final text; clear the slot.
    if (slot) {
      slot.classList.remove("interim");
      slot.querySelector(".what").textContent = evt.text;
      slot.querySelector(".ts").textContent = fmtClock(evt.ts_start);
      interimSlot[evt.channel] = null;
    } else {
      pane.appendChild(buildUtt(evt));
    }
  }

  if (autoScroll) pane.scrollTop = pane.scrollHeight;
}

function clearPane() {
  pane.innerHTML = "";
  interimSlot.mic = null;
  interimSlot.system = null;
}

// --- status bar ---

async function refreshStatus() {
  try {
    const resp = await fetch("/api/status");
    const s = await resp.json();
    statusEl.classList.toggle("recording", !!s.active);
    statusEl.classList.toggle("idle", !s.active);
    statusEl.classList.toggle("warn", !!s.transcription_failed);
    if (s.active) {
      const warn = s.transcription_failed
        ? ' <span class="meta warn">transcription failed</span>'
        : "";
      statusLabel.innerHTML =
        `recording <span class="meta">${s.slug || ""}</span>${warn}`;
    } else {
      statusLabel.textContent = "idle";
    }
  } catch {
    statusLabel.textContent = "offline";
  }
}
setInterval(refreshStatus, 3000);

// --- WebSocket ---

function connectWs() {
  if (ws && ws.readyState === WebSocket.OPEN) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${proto}://${location.host}/ws`);
  ws = socket;
  socket.addEventListener("open", () => {
    wsBackoff = 500;
    lastMsgAt = Date.now();
    if (livenessTimer) clearInterval(livenessTimer);
    livenessTimer = setInterval(() => {
      if (Date.now() - lastMsgAt > LIVENESS_TIMEOUT_MS) {
        try { socket.close(); } catch {}
      }
    }, 5000);
  });
  socket.addEventListener("message", (ev) => {
    lastMsgAt = Date.now();
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === "event") handleEvent(msg);
      else if (msg.type === "session_end") {
        // Meeting view re-renders statically via the close handler; only
        // the live pane wipes here.
        if (location.hash === "" || location.hash === "#/live") clearPane();
      }
    } catch {}
  });
  socket.addEventListener("close", () => {
    // Stale close handlers (from sockets we've already replaced or
    // disconnected) shouldn't drive reconnect.
    if (ws !== socket) return;
    if (livenessTimer) { clearInterval(livenessTimer); livenessTimer = null; }
    const h = location.hash;
    if (h === "" || h === "#/live") {
      setTimeout(connectWs, wsBackoff);
      wsBackoff = Math.min(wsBackoff * 2, 10000);
    } else if (h.startsWith("#/meeting/")) {
      // Re-route: renderMeeting will reopen the ws if the meeting is
      // still active, or fall through to the static render if it ended.
      setTimeout(route, wsBackoff);
      wsBackoff = Math.min(wsBackoff * 2, 10000);
    }
  });
  socket.addEventListener("error", () => {
    try { socket.close(); } catch {}
  });
}

function disconnectWs() {
  if (ws) {
    try { ws.close(); } catch {}
    ws = null;
  }
}

// --- Router ---

async function renderLive() {
  disconnectWs();
  clearPane();
  pane.style.display = "block";
  connectWs();
}

async function renderList() {
  disconnectWs();
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
  disconnectWs();
  clearPane();
  pane.innerHTML = `<h1>loading…</h1>`;

  // For the active meeting, stream live instead of rendering a static
  // snapshot. /ws flushes transcript.jsonl as backlog and then live-streams,
  // so we get everything the live pane would. Summary + audio are produced
  // post-end; the close handler re-renders statically once the session ends.
  const status = await fetch("/api/status")
    .then(r => r.ok ? r.json() : {})
    .catch(() => ({}));
  if (status.active && status.slug === slug) {
    const info = await fetch(`/api/meetings/${slug}`)
      .then(r => r.ok ? r.json() : {})
      .catch(() => ({}));
    pane.innerHTML = "";
    const h1 = document.createElement("h1");
    h1.textContent = info?.title || slug;
    pane.appendChild(h1);
    connectWs();
    return;
  }

  const [info, transcript, summaryRes] = await Promise.all([
    fetch(`/api/meetings/${slug}`).then(r => r.ok ? r.json() : {}),
    fetch(`/api/meetings/${slug}/transcript`).then(r => r.ok ? r.json() : []),
    fetch(`/api/meetings/${slug}/summary`).then(r => r.ok ? r.json() : null),
  ]);
  const summaryMd = summaryRes?.markdown ?? null;
  const title = info?.title || slug;
  pane.innerHTML = `
    <h1>${escHtml(title)}</h1>
    ${summaryMd ? `<details class="summary" open><summary>summary</summary><div class="summary-body">${summaryToHtml(summaryMd)}</div></details>` : ""}
    <p><audio controls src="/api/meetings/${slug}/audio" style="width:100%;max-width:40rem;"></audio></p>
    <div id="utts"></div>
  `;
  const utts = pane.querySelector("#utts");
  for (const evt of transcript) {
    utts.appendChild(buildUtt(evt));
  }
}

async function renderUnknowns() {
  disconnectWs();
  pane.innerHTML = "<h1>identify speakers</h1><p>loading…</p>";
  let unknowns;
  try {
    const resp = await fetch("/api/unknowns");
    unknowns = await resp.json();
  } catch (e) {
    pane.innerHTML = `<h1>identify speakers</h1><p class="error">failed to load: ${escHtml(String(e))}</p>`;
    return;
  }
  if (!unknowns.length) {
    pane.innerHTML = `<h1>identify speakers</h1><p>nothing to identify — every captured voiceprint is bound to a name.</p>`;
    return;
  }
  pane.innerHTML = `
    <h1>identify speakers</h1>
    <p class="muted">${unknowns.length} unbound voice${unknowns.length === 1 ? "print" : "prints"}, sorted by total speaking time. Listen to each clip, then bind it to a real person — that fixes every meeting they appeared in and auto-labels them in future meetings.</p>
    <ul id="unknowns" class="unknowns"></ul>
  `;
  const ul = pane.querySelector("#unknowns");
  for (const u of unknowns) {
    ul.appendChild(buildUnknownCard(u));
  }
}

function buildUnknownCard(u) {
  const li = document.createElement("li");
  li.className = "unknown-card";
  li.dataset.hash = u.hash;

  const head = document.createElement("div");
  head.className = "unknown-head";
  const title = document.createElement("div");
  title.className = "unknown-title";
  const minutes = (u.total_seconds / 60).toFixed(1);
  const where = u.n_meetings === 1
    ? `1 meeting`
    : `${u.n_meetings} meetings`;
  title.innerHTML = `<code>unknown_${escHtml(u.hash)}</code> <span class="muted">· ${minutes}m across ${where}</span>`;
  head.appendChild(title);
  if (u.current_label) {
    const cur = document.createElement("div");
    cur.className = "current-label";
    cur.textContent = `currently labeled: ${u.current_label}`;
    head.appendChild(cur);
  }
  li.appendChild(head);

  const ctx = document.createElement("div");
  ctx.className = "muted";
  const dt = u.primary.started_at
    ? new Date(u.primary.started_at).toLocaleString()
    : "";
  ctx.innerHTML = `from <a href="#/meeting/${encodeURIComponent(u.primary.slug)}">${escHtml(u.primary.title)}</a> · ${escHtml(dt)} · ${escHtml(u.primary.speaker_id)}`;
  li.appendChild(ctx);

  const samples = document.createElement("ul");
  samples.className = "samples";
  for (const s of u.primary.samples) {
    const sli = document.createElement("li");
    sli.textContent = s;
    samples.appendChild(sli);
  }
  li.appendChild(samples);

  const audio = document.createElement("audio");
  audio.controls = true;
  audio.preload = "none";
  audio.src = `/api/unknowns/${encodeURIComponent(u.hash)}/clip.mp3`;
  li.appendChild(audio);

  const form = document.createElement("form");
  form.className = "bind-form";
  const input = document.createElement("input");
  input.type = "text";
  input.className = "bind-name";
  input.placeholder = "name";
  input.required = true;
  // Chrome ignores autocomplete="off" on text inputs and pops its own
  // saved-value history. Omitting the form field name + using a
  // non-standard autocomplete token tells it to back off.
  input.autocomplete = "new-password";
  const btn = document.createElement("button");
  btn.type = "submit";
  btn.textContent = "bind";
  const status = document.createElement("span");
  status.className = "bind-status";
  form.appendChild(input);
  form.appendChild(btn);
  form.appendChild(status);
  form.addEventListener("submit", (e) => onBindSubmit(e, u, li));
  li.appendChild(form);

  if (u.candidates && u.candidates.length) {
    const chips = document.createElement("div");
    chips.className = "candidate-chips";
    for (const c of u.candidates) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "candidate-chip";
      chip.textContent = c;
      chip.addEventListener("click", () => {
        input.value = c;
        input.focus();
      });
      chips.appendChild(chip);
    }
    li.appendChild(chips);
  }

  return li;
}

async function onBindSubmit(e, u, card) {
  e.preventDefault();
  const form = e.target;
  const input = form.querySelector('input.bind-name');
  const btn = form.querySelector('button');
  const status = form.querySelector('.bind-status');
  const name = input.value.trim();
  if (!name) return;
  btn.disabled = true;
  input.disabled = true;
  status.textContent = "binding…";
  status.classList.remove("error");
  try {
    const resp = await fetch(`/api/unknowns/${encodeURIComponent(u.hash)}/bind`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    if (!resp.ok) {
      const detail = await resp.json().catch(() => ({}));
      throw new Error(detail.detail || `HTTP ${resp.status}`);
    }
    const result = await resp.json();
    card.classList.add("bound");
    const absorbed = result.absorbed || [];
    const meetingsTouched = result.updated_meetings.length
      + absorbed.reduce((n, a) => n + (a.meetings ? a.meetings.length : 0), 0);
    let absorbedNote = "";
    if (absorbed.length) {
      absorbedNote = ` · also folded in ${absorbed.length} similar voiceprint${absorbed.length === 1 ? "" : "s"}`;
      // Remove the absorbed unknowns' cards — they no longer exist on disk.
      for (const a of absorbed) {
        const h = a.label.replace(/^unknown_/, "");
        const stale = document.querySelector(`.unknown-card[data-hash="${h}"]`);
        if (stale) stale.remove();
      }
    }
    card.innerHTML = `<div class="bound-msg">✓ bound <strong>unknown_${escHtml(u.hash)}</strong> → <strong>${escHtml(result.name)}</strong> (updated ${meetingsTouched} meeting${meetingsTouched === 1 ? "" : "s"}${absorbedNote})</div>`;
  } catch (err) {
    btn.disabled = false;
    input.disabled = false;
    status.textContent = String(err.message || err);
    status.classList.add("error");
  }
}

function route() {
  const h = location.hash;
  if (h === "" || h === "#/live") return renderLive();
  if (h === "#/meetings") return renderList();
  if (h === "#/unknowns") return renderUnknowns();
  const m = h.match(/^#\/meeting\/(.+)$/);
  if (m) return renderMeeting(decodeURIComponent(m[1]));
  renderLive();
}
window.addEventListener("hashchange", route);

jumpBtn.addEventListener("click", () => {
  pane.scrollTop = pane.scrollHeight;
  autoScroll = true;
  jumpBtn.hidden = true;
});
pane.addEventListener("scroll", () => {
  const atBottom = Math.abs(
    pane.scrollHeight - pane.scrollTop - pane.clientHeight
  ) < 40;
  if (atBottom !== autoScroll) {
    autoScroll = atBottom;
    jumpBtn.hidden = autoScroll;
  }
});

refreshStatus();
route();
