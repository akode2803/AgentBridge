/* AgentBridge GUI front-end — vanilla JS single page, hash routing. */

"use strict";

const $ = (sel) => document.querySelector(sel);

const App = {
  state: null,          // last /api/state payload
  page: null,
  logKey: "",           // change detector so the transcript only re-renders on news
  wizard: { step: 0, shared: "", validated: null, role: "claude", peer: "coco",
            initDone: false, skills: null },
};

// ---------------------------------------------------------------- helpers

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  const r = await fetch(path, opts);
  return r.json();
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

let toastTimer = null;
function toast(msg, isError) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = isError ? "error" : "";
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 3200);
}

// ---------------------------------------------------------------- chrome

function channelStateText(s) {
  if (!s.configured) return ["dot-grey", "not configured — run Setup"];
  if (s.paused) return ["dot-red", "PAUSED via control.json"];
  if (!s.shared_ok) return ["dot-red", "shared folder unreachable"];
  if (s.inbound_waiting) return ["dot-orange", `inbound waiting: ${s.peer} seq ${s.inbound_seq}`];
  if (s.outbound_undelivered) return ["dot-orange", `outbound in flight: seq ${s.me.seq} not yet acked`];
  return ["dot-green", "channel idle — all messages delivered"];
}

function renderChrome() {
  const s = App.state;
  if (!s) return;
  const chip = $("#channel-chip");
  if (s.configured) {
    chip.hidden = false;
    chip.textContent = `${s.role} ⇄ ${s.peer}`;
  } else {
    chip.hidden = true;
  }
  $("#paused-badge").hidden = !s.paused;
  const [dot, text] = channelStateText(s);
  $("#status-dot").className = "dot " + dot;
  $("#status-text").textContent = text;
  $("#status-versions").textContent =
    `GUI v${s.gui_version} · bridge v${s.bridge_version}`;
  $("#nav-inbound-dot").hidden = !s.inbound_waiting;
  document.querySelectorAll("#navrail a").forEach((a) => {
    a.classList.toggle("active", a.dataset.page === App.page);
  });
}

// ---------------------------------------------------------------- pages

function renderHome() {
  const s = App.state;
  if (!s.configured) {
    $("#content").innerHTML = `
      <h1>Welcome to AgentBridge</h1>
      <p class="page-sub">The bridge is not configured on this machine yet.</p>
      <div class="card" style="max-width:560px">
        <p>AgentBridge connects your local Claude to a remote agent through a
        OneDrive/SharePoint synced folder — no credentials, no servers.</p>
        <button class="primary" onclick="location.hash='#/setup'">Open the setup wizard</button>
      </div>`;
    return;
  }
  const [dot, text] = channelStateText(s);
  const peerEnv = s.peer_env;
  $("#content").innerHTML = `
    <h1>Home</h1>
    <p class="page-sub">Bridge status at a glance.</p>
    <div class="statepill"><span class="dot ${dot}"></span>${esc(text)}</div>
    <div class="card-grid">
      <div class="card">
        <h2>Channel</h2>
        <dl class="kv">
          <dt>Me (${esc(s.role)})</dt><dd>seq ${s.me.seq} · ack ${s.me.ack}</dd>
          <dt>Last sent</dt><dd>${esc(s.me.ts_local || "never")}</dd>
          <dt>Peer (${esc(s.peer)})</dt>
          <dd>${peerEnv ? `seq ${peerEnv.seq} · ack ${peerEnv.ack}` : "no envelope yet — peer has never sent"}</dd>
          <dt>Peer last sent</dt><dd>${esc(peerEnv?.ts_local || "—")}</dd>
          <dt>Peer app</dt><dd>${peerEnv ? "v" + esc(peerEnv.app_version || "?") : "—"}</dd>
        </dl>
      </div>
      <div class="card">
        <h2>Connection</h2>
        <dl class="kv">
          <dt>Shared folder</dt><dd class="mono">${esc(s.shared_dir)}</dd>
          <dt>Reachable</dt><dd>${s.shared_ok ? "✓ yes" : "✗ NO — check OneDrive"}</dd>
          <dt>OneDrive client</dt><dd>${s.onedrive_running === null ? "unknown" : s.onedrive_running ? "✓ running" : "✗ not running"}</dd>
          <dt>Poll interval</dt><dd>${s.poll}s</dd>
          <dt>Handler</dt><dd>${esc(s.handler_cmd || "none (this side is human/Claude-driven)")}</dd>
        </dl>
      </div>
      <div class="card">
        <h2>Controls</h2>
        <div class="row" style="margin-bottom:12px">
          <label class="switch">
            <input type="checkbox" id="pause-toggle" ${s.paused ? "checked" : ""}>
            <span class="slider"></span>
          </label>
          <span><b>Kill switch</b> — pause both agents (control.json)</span>
        </div>
        <div class="row">
          <button onclick="openTarget('shared')">Open shared folder</button>
          <button onclick="openTarget('files')">Attachments</button>
          <button onclick="openTarget('inbox')">Inbox copies</button>
        </div>
      </div>
    </div>`;
  $("#pause-toggle").addEventListener("change", async (e) => {
    const r = await api("/api/pause", { paused: e.target.checked });
    if (r.error) toast(r.error, true);
    else toast(r.paused ? "Bridge PAUSED for both agents" : "Bridge resumed");
    refresh(true);
  });
}

async function openTarget(target) {
  const r = await api("/api/open", { target });
  if (r.error) toast(r.error, true);
}

async function renderMessages(force) {
  const s = App.state;
  if (!s.configured) { location.hash = "#/setup"; return; }
  const log = await api(`/api/log?tail=200`);
  const inbound = s.inbound_waiting ? await api("/api/inbound") : { waiting: false };
  const key = JSON.stringify([log.entries.length,
    log.entries.at(-1)?.seq, log.entries.at(-1)?.from, inbound.seq]);
  if (!force && key === App.logKey && App.page === "messages") return;
  App.logKey = key;

  const bubbles = log.entries.map((e) => {
    const files = (e.files || []).map((f) =>
      `<span class="file-chip">📎 ${esc(f.name)} · ${f.bytes ?? "?"} B</span>`).join(" ");
    return `
      <div class="msg ${e.mine ? "mine" : ""}">
        <div class="bubble">${esc(e.body)}${files ? "<br>" + files : ""}</div>
        <div class="meta">${esc(e.from)} · seq ${e.seq} · ${esc(e.type || "chat")} · ${esc(e.ts_local || "")}</div>
      </div>`;
  }).join("") || `<div class="empty">No messages yet.</div>`;

  const banner = inbound.waiting ? `
    <div class="banner">
      <span>⬇ <b>Unacknowledged message</b> from ${esc(inbound.from)}
        (seq ${inbound.seq}, ${esc(inbound.type || "chat")}) — your Claude session
        will pick it up with <code>recv --mark</code>, or acknowledge it here.</span>
      <span class="spacer"></span>
      <button class="primary" id="ack-btn">Mark read</button>
    </div>` : "";

  $("#content").innerHTML = `
    <h1>Messages</h1>
    <p class="page-sub">Transcript from the shared audit logs (${esc(log.role)} ⇄ ${esc(log.peer)}).</p>
    ${banner}
    <div id="transcript">${bubbles}</div>
    <div id="composer">
      <textarea id="compose-body" placeholder="Message ${esc(s.peer)}…  (Ctrl+Enter to send)"></textarea>
      <select id="compose-type">
        <option>chat</option><option>task</option><option>result</option>
        <option>control</option><option>ping</option>
      </select>
      <button class="primary" id="send-btn">Send</button>
    </div>`;

  const doSend = async () => {
    const body = $("#compose-body").value.trim();
    if (!body) return;
    $("#send-btn").disabled = true;
    const r = await api("/api/send", { body, type: $("#compose-type").value });
    $("#send-btn").disabled = false;
    if (r.error) { toast(r.error, true); return; }
    $("#compose-body").value = "";
    toast(`Sent seq ${r.seq} to ${s.peer}`);
    refresh(true);
  };
  $("#send-btn").addEventListener("click", doSend);
  $("#compose-body").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.ctrlKey) doSend();
  });
  const ackBtn = $("#ack-btn");
  if (ackBtn) ackBtn.addEventListener("click", async () => {
    const r = await api("/api/ack", {});
    if (r.error) toast(r.error, true);
    else toast(`Acknowledged seq ${r.seq}`);
    refresh(true);
  });
  const tr = $("#transcript");
  tr.scrollTop = tr.scrollHeight;
  window.scrollTo(0, document.body.scrollHeight);
}

function renderActivity() {
  $("#content").innerHTML = `
    <h1>Activity</h1>
    <p class="page-sub">Live view of what the remote agent is doing.</p>
    <div class="card" style="max-width:640px">
      <h2>Livestream — coming next</h2>
      <p>When CoCo runs a task, its handler will tail the Cortex
      <code>stream-json</code> events into a status file in the shared folder.
      This pane will render that feed as a live progress view, so anyone can
      see "is it working?" without reading logs.</p>
      <p class="hint">Requires the handler-side feed writer (planned) — nothing
      to configure here yet.</p>
      <div class="empty">Waiting for a livestream feed…</div>
    </div>`;
}

function renderSettings() {
  const s = App.state;
  const cfg = s.configured ? {
    role: s.role, peer: s.peer, shared_dir: s.shared_dir,
    poll_seconds: s.poll, handler_cmd: s.handler_cmd || undefined,
  } : null;
  $("#content").innerHTML = `
    <h1>Settings</h1>
    <p class="page-sub">Local bridge configuration on this machine.</p>
    <div class="card" style="max-width:640px">
      <h2>Config <span class="mono" style="text-transform:none">(${esc(s.home)}\\config.json)</span></h2>
      ${cfg ? `<pre class="configdump mono">${esc(JSON.stringify(cfg, null, 2))}</pre>`
            : `<div class="empty">Not configured — run the setup wizard.</div>`}
      <p class="hint">To change role, peer or the shared folder, re-run the
      <a href="#/setup">setup wizard</a> — it preserves any handler settings.
      Power users can keep using the CLI: <code>python bridge.py …</code></p>
    </div>`;
}

// ---------------------------------------------------------------- wizard

const WIZ_STEPS = ["Welcome", "System checks", "Shared folder", "Identity",
                   "Create bridge", "Install skills", "Done"];

function renderSetup() {
  const w = App.wizard;
  const stepsHtml = WIZ_STEPS.map((name, i) => `
    <li class="${i === w.step ? "current" : i < w.step ? "done" : ""}">
      <span class="n">${i < w.step ? "✓" : i + 1}</span> ${name}
    </li>`).join("");
  $("#content").innerHTML = `
    <h1>Setup</h1>
    <p class="page-sub">Guided onboarding for this machine's side of the bridge.</p>
    <div id="wizard">
      <div id="wizard-steps" class="card"><ol>${stepsHtml}</ol></div>
      <div id="wizard-body"></div>
    </div>`;
  renderWizardStep();
}

function wizardNav(backOk, nextOk, nextLabel) {
  return `<div class="wizard-nav">
    ${backOk ? `<button id="wiz-back">Back</button>` : ""}
    ${nextOk ? `<button class="primary" id="wiz-next">${nextLabel || "Next"}</button>` : ""}
  </div>`;
}

function bindWizardNav(onNext) {
  const back = $("#wiz-back");
  if (back) back.addEventListener("click", () => { App.wizard.step--; renderSetup(); });
  const next = $("#wiz-next");
  if (next) next.addEventListener("click", async () => {
    if (onNext && await onNext() === false) return;
    App.wizard.step++;
    renderSetup();
  });
}

async function renderWizardStep() {
  const w = App.wizard;
  const body = $("#wizard-body");
  const s = App.state;

  if (w.step === 0) {
    body.innerHTML = `<div class="card">
      <h2>Welcome</h2>
      <p>This wizard sets up <b>your side</b> of an AgentBridge — the message
      bus that connects your local Claude to a remote agent through a synced
      OneDrive/SharePoint folder.</p>
      <p>It will: check your system, point the bridge at the shared folder,
      create the local config, and install the Claude Code skills your
      sessions use to drive the bridge.</p>
      ${s.configured ? `<p class="hint">⚠ This machine is already configured
        (${esc(s.role)} ⇄ ${esc(s.peer)}). Re-running the wizard will update the
        config; handler settings are preserved.</p>` : ""}
      ${wizardNav(false, true, "Start")}</div>`;
    bindWizardNav();
  }

  else if (w.step === 1) {
    body.innerHTML = `<div class="card"><h2>System checks</h2>
      <ul class="checklist" id="checks"><li>Running checks…</li></ul>
      ${wizardNav(true, true)}</div>`;
    bindWizardNav();
    const d = await api("/api/doctor");
    $("#checks").innerHTML = d.checks.map((c) => `
      <li><span class="${c.ok ? "ok" : "fail"}">${c.ok ? "✓" : "✗"}</span>
        ${esc(c.label)} <span class="detail">${esc(c.detail || "")}</span></li>`).join("");
  }

  else if (w.step === 2) {
    body.innerHTML = `<div class="card"><h2>Shared folder</h2>
      <p>Pick the locally-synced copy of the shared SharePoint folder
      (add it in OneDrive first via <i>"Add shortcut to My files"</i>).</p>
      <div class="row">
        <input type="text" id="shared-input" style="flex:1;min-width:280px"
               placeholder="C:\\Users\\you\\OneDrive - …" value="${esc(w.shared)}">
        <button id="browse-btn">Browse…</button>
      </div>
      <p id="validate-result" class="hint">${w.validated?.ok
        ? `<span class="result-ok">✓ folder is writable${w.validated.looks_synced ? " and looks synced" : ""}</span>` : ""}</p>
      ${wizardNav(true, true)}</div>`;
    $("#browse-btn").addEventListener("click", async () => {
      toast("Folder picker opened — check your taskbar");
      const r = await api("/api/pick_folder", {});
      if (r.path) { $("#shared-input").value = r.path; }
    });
    bindWizardNav(async () => {
      const path = $("#shared-input").value.trim();
      const v = await api("/api/validate_shared", { path });
      if (!v.ok) {
        $("#validate-result").innerHTML =
          `<span class="result-bad">✗ ${esc(v.detail || "invalid folder")}</span>`;
        return false;
      }
      w.shared = v.path; w.validated = v;
      if (!v.looks_synced) toast("Note: path doesn't look like a OneDrive/SharePoint folder", true);
      return true;
    });
  }

  else if (w.step === 3) {
    body.innerHTML = `<div class="card"><h2>Identity</h2>
      <p>Who is this machine, and who is the peer? The defaults fit the
      standard analyst setup (your Claude ⇄ remote CoCo).</p>
      <dl class="kv" style="max-width:380px">
        <dt>My role</dt><dd><input type="text" id="role-input" value="${esc(w.role)}"></dd>
        <dt>Peer role</dt><dd><input type="text" id="peer-input" value="${esc(w.peer)}"></dd>
      </dl>
      <p class="hint">Custom names work too (e.g. analyst ⇄ sqlbot) — any pair,
      as long as they differ.</p>
      ${wizardNav(true, true)}</div>`;
    bindWizardNav(async () => {
      const role = $("#role-input").value.trim();
      const peer = $("#peer-input").value.trim();
      if (!role || !peer || role === peer) {
        toast("Role and peer must be non-empty and different", true);
        return false;
      }
      w.role = role; w.peer = peer;
      return true;
    });
  }

  else if (w.step === 4) {
    body.innerHTML = `<div class="card"><h2>Create bridge</h2>
      <dl class="kv">
        <dt>Role</dt><dd>${esc(w.role)} ⇄ ${esc(w.peer)}</dd>
        <dt>Shared folder</dt><dd class="mono">${esc(w.shared)}</dd>
        <dt>Config location</dt><dd class="mono">${esc(s.home)}</dd>
      </dl>
      <p id="init-result" class="hint">${w.initDone ? '<span class="result-ok">✓ bridge created</span>' : ""}</p>
      ${wizardNav(true, true, w.initDone ? "Next" : "Create")}</div>`;
    bindWizardNav(async () => {
      if (w.initDone) return true;
      const r = await api("/api/init", { role: w.role, peer: w.peer, shared: w.shared });
      if (r.error) {
        $("#init-result").innerHTML = `<span class="result-bad">✗ ${esc(r.error)}</span>`;
        return false;
      }
      w.initDone = true;
      await refresh(true);
      return true;
    });
  }

  else if (w.step === 5) {
    body.innerHTML = `<div class="card"><h2>Install skills</h2>
      <p>Installs the AgentBridge skills into <code>~\\.claude\\skills</code> so
      your Claude Code sessions know how to drive the bridge.</p>
      <p id="skills-result" class="hint">${w.skills
        ? `<span class="result-ok">✓ installed: ${esc(w.skills.join(", "))}</span>` : ""}</p>
      <p class="hint">Heads-up: this covers Claude Code (CLI/desktop/IDE).
      claude.ai chat needs the pre-built zips uploaded via
      Settings → Capabilities → Skills.</p>
      ${wizardNav(true, true, w.skills ? "Next" : "Install")}</div>`;
    bindWizardNav(async () => {
      if (w.skills) return true;
      const r = await api("/api/install_skills", {});
      if (r.error) {
        $("#skills-result").innerHTML = `<span class="result-bad">✗ ${esc(r.error)}</span>`;
        return false;
      }
      w.skills = r.installed;
      return true;
    });
  }

  else {
    body.innerHTML = `<div class="card"><h2>All set 🎉</h2>
      <p>This machine is configured${w.role ? ` as <b>${esc(w.role)}</b>` : ""}.
      Next steps:</p>
      <ul>
        <li>Set up the <b>remote side</b> on the peer machine — follow
          <code>REMOTE_SETUP.md</code> (the wizard will cover this soon).</li>
        <li>Send a first <code>ping</code> from the Messages page.</li>
      </ul>
      <div class="wizard-nav">
        <button class="primary" onclick="location.hash='#/home'">Go to Home</button>
        <button id="wiz-restart">Run wizard again</button>
      </div></div>`;
    $("#wiz-restart").addEventListener("click", () => {
      App.wizard = { step: 0, shared: "", validated: null, role: "claude",
                     peer: "coco", initDone: false, skills: null };
      renderSetup();
    });
  }
}

// ---------------------------------------------------------------- router/loop

const PAGES = {
  home: renderHome,
  messages: () => renderMessages(true),
  activity: renderActivity,
  setup: renderSetup,
  settings: renderSettings,
};

function route() {
  const page = (location.hash.replace("#/", "") || "home");
  App.page = PAGES[page] ? page : "home";
  renderChrome();
  PAGES[App.page]();
}

async function refresh(rerender) {
  try {
    App.state = await api("/api/state");
  } catch {
    $("#status-dot").className = "dot dot-red";
    $("#status-text").textContent = "GUI server not reachable";
    return;
  }
  renderChrome();
  if (rerender && App.page !== "setup") PAGES[App.page]();
  else if (App.page === "messages") renderMessages(false);
  else if (App.page === "home") renderHome();
}

window.addEventListener("hashchange", route);

(async function start() {
  App.state = await api("/api/state");
  if (!App.state.configured && !location.hash) location.hash = "#/setup";
  route();
  setInterval(() => refresh(false), 2500);
})();
