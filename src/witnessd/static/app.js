// witness — meeting browser
//
// Routes (hash-based, no framework):
//   #/meetings or empty      → list of past meetings
//   #/meeting/<slug>         → transcript + summary + audio for one meeting
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
    <p><audio controls src="/api/meetings/${slug}/audio" style="width:100%;max-width:40rem;"></audio></p>
    <div id="utts"></div>
  `;
  const utts = pane.querySelector("#utts");
  for (const evt of transcript) {
    utts.appendChild(buildUtt(evt));
  }
}

function route() {
  const h = location.hash;
  const m = h.match(/^#\/meeting\/(.+)$/);
  if (m) return renderMeeting(decodeURIComponent(m[1]));
  return renderList();
}
window.addEventListener("hashchange", route);

refreshStatus();
route();
