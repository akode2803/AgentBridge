/* Settings pages — profile, account, chats (theme), my agents, connection.
   The section nav lives in the sidebar. */

import { $, esc, toast, setTheme } from "./util.js";
import { ICONS } from "./icons.js";
import { api } from "./api.js";
import { csel } from "./csel.js";
import { App, Mesh, Settings, RULE_LABELS, meshDn, renderChrome } from "./state.js";
import { renderSidebar } from "./sidebar.js";
import { V } from "./views.js";

async function renderSettings() {
  const s = App.state;
  if (!s.configured) { location.hash = "#/setup"; return; }
  // render from the cached mesh state so the swap is synchronous with the
  // route change — awaiting a fresh fetch here left the previous chat on
  // screen (minus its chat-mode class) for a visible ~300ms (stutter). A
  // background refresh keeps a long-lived settings page current.
  const ms = Mesh.state || (Mesh.state = await api("/api/mesh/state"));
  if (!ms.available || !ms.user) { location.hash = "#/chats"; return; }
  api("/api/mesh/state").then((fresh) => {
    if (fresh && !fresh.error && App.page === "settings") Mesh.state = fresh;
  }).catch(() => {});
  renderSidebar();
  $("#details-pane").hidden = true;
  const section = Settings.section || "profile";
  const dark = document.documentElement.dataset.theme === "dark";
  const back = `<button class="mob-back" onclick="location.hash='#/settings'">${ICONS.back} Settings</button>`;

  let html = "";
  if (section === "profile") {
    html = `${back}<h1>Profile</h1>
      <div class="card" style="max-width:560px">
        <div style="display:flex;align-items:center;gap:16px">
          <span class="acct-big" style="width:64px;height:64px;font-size:26px;border-radius:50%;background:var(--accent);color:#fff;display:grid;place-items:center;font-weight:700">${esc((meshDn(ms.user)[0] || "?").toUpperCase())}</span>
          <div>
            <div style="font-weight:600;font-size:16px">${esc(meshDn(ms.user))}</div>
            <div class="hint">@${esc(ms.user)} · member</div>
          </div>
        </div>
        <p class="hint" style="margin-bottom:0">Display name and profile photo
        editing arrive with the account overhaul.</p>
      </div>`;
  } else if (section === "account") {
    html = `${back}<h1>Account</h1>
      <div class="card" style="max-width:560px">
        <h2>Signed in as</h2>
        <dl class="kv">
          <dt>Name</dt><dd><b>${esc(meshDn(ms.user))}</b></dd>
          <dt>Username</dt><dd>@${esc(ms.user)}</dd>
        </dl>
        <div class="row" style="margin-top:12px"><button id="st-logout">Sign out</button></div>
        <p class="hint" style="margin-bottom:0">Your account lives in the shared
        folder — it works from any machine that syncs it. Password change is
        coming with the account overhaul.</p>
      </div>`;
  } else if (section === "chats") {
    html = `${back}<h1>Chats</h1>
      <div class="card" style="max-width:560px">
        <h2>Appearance</h2>
        <div class="row">
          <label class="switch">
            <input type="checkbox" id="theme-toggle" ${dark ? "checked" : ""}>
            <span class="slider"></span>
          </label>
          <span><b>Dark mode</b></span>
        </div>
        <p class="hint" style="margin-bottom:0">Full theming and wallpapers come
        with the theming pass.</p>
      </div>`;
  } else if (section === "agents") {
    const mine = Object.values(ms.users)
      .filter((u) => u.kind === "agent" && (u.owners || []).includes(ms.user));
    html = `${back}<h1>My agents</h1>
      ${mine.map((a) => {
        const st = a.settings || {};
        return `
        <div class="card" style="max-width:640px">
          <h2>${esc(a.display)} <span class="hint" style="text-transform:none">@${esc(a.username)}</span></h2>
          <dl class="kv" style="grid-template-columns:minmax(110px,160px) 1fr">
            <dt>Model</dt><dd><input type="text" class="ag-model" data-agent="${esc(a.username)}"
              value="${esc(st.model || "")}" placeholder="agent default"></dd>
            <dt>Reasoning effort</dt><dd><input type="text" class="ag-reason" data-agent="${esc(a.username)}"
              value="${esc(st.reasoning || "")}" placeholder="agent default"></dd>
            <dt>Default reply rule</dt><dd><select class="ag-default" data-agent="${esc(a.username)}">
              ${Object.entries(RULE_LABELS).map(([r, label]) =>
                `<option value="${r}" ${(st.default_rule || "tagged") === r ? "selected" : ""}>${label}</option>`).join("")}
            </select></dd>
            <dt>Owners</dt><dd>${(a.owners || []).map((o) => esc("@" + o)).join(", ")}</dd>
          </dl>
          <p class="hint">Per-chat rules live in each chat's info page.</p>
          <div class="row"><button class="primary ag-save" data-agent="${esc(a.username)}">Save</button></div>
        </div>`;
      }).join("") || ""}
      <div class="card" style="max-width:640px">
        <h2>Add an agent</h2>
        <div class="row">
          <input type="text" id="new-agent-user" placeholder="username (e.g. coco2)">
          <input type="text" id="new-agent-display" placeholder="Display name">
          <button class="primary" id="new-agent-btn">Create</button>
        </div>
        <p class="hint" style="margin-bottom:0">You become its responsible member;
        its machine runs <code>agent_worker.py</code>.</p>
      </div>
      <div class="card" style="max-width:640px">
        <h2>Emergency stand-down</h2>
        <div class="row">
          <label class="switch">
            <input type="checkbox" id="st-pause" ${ms.paused ? "checked" : ""}>
            <span class="slider"></span>
          </label>
          <span><b>Stand down all agents</b> — every agent in every chat
          holds until resumed</span>
        </div>
        <p class="hint" style="margin-bottom:0">Any member can flip this.
        Pending requests get one consolidated reply per chat after
        resuming.</p>
      </div>`;
  } else if (section === "connection") {
    html = `${back}<h1>Connection</h1>
      <div class="card" style="max-width:640px">
        <dl class="kv">
          <dt>Shared folder</dt><dd class="mono">${esc(s.shared_dir)}
            <a href="#" id="open-shared2">open</a></dd>
          <dt>Folder synced</dt><dd>${s.shared_ok ? "✓ Yes" : "✗ No — check OneDrive"}</dd>
          <dt>Sync client</dt><dd>${s.onedrive_running === null ? "Unknown" : s.onedrive_running ? "✓ Running" : "✗ Not running"}</dd>
          <dt>Versions</dt><dd>App v${esc(s.gui_version)} · Bridge v${esc(s.bridge_version)}</dd>
        </dl>
        <div class="row" style="margin-top:10px">
          <button onclick="openTarget('home')">Open config folder</button>
        </div>
      </div>
      <div class="card" style="max-width:640px">
        <h2>Performance</h2>
        <dl class="kv" style="grid-template-columns:minmax(110px,160px) 1fr">
          <dt>Check for news</dt><dd><span id="poll-slot"></span></dd>
        </dl>
        <p class="hint" style="margin-bottom:0">How often this window re-reads
        the shared folder. Faster feels snappier; slower is lighter on OneDrive
        and disk. Applies from the next tick — no restart needed.</p>
      </div>`;
  }
  $("#content").innerHTML = `<div class="settings-body">${html}</div>`;

  const theme = $("#theme-toggle");
  if (theme) theme.addEventListener("change", (e) => {
    setTheme(e.target.checked ? "dark" : "light");
  });
  const logout = $("#st-logout");
  if (logout) logout.addEventListener("click", async () => {
    await api("/api/mesh/logout", {});
    location.hash = "#/chats";
  });
  const shared2 = $("#open-shared2");
  if (shared2) shared2.addEventListener("click", (e) => {
    e.preventDefault(); window.openTarget("shared");
  });
  const pollSlot = $("#poll-slot");
  if (pollSlot) pollSlot.appendChild(csel({
    options: [
      { v: "1000", label: "Every second" },
      { v: "2500", label: "Every 2.5 seconds (default)" },
      { v: "5000", label: "Every 5 seconds" },
      { v: "10000", label: "Every 10 seconds" },
      { v: "30000", label: "Every 30 seconds" },
    ],
    value: localStorage.getItem("pollMs") || "2500",
    onChange: (v) => localStorage.setItem("pollMs", v),
  }));
  document.querySelectorAll(".ag-save").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const agent = btn.dataset.agent;
      const patch = {
        model: document.querySelector(`.ag-model[data-agent="${agent}"]`).value.trim() || null,
        reasoning: document.querySelector(`.ag-reason[data-agent="${agent}"]`).value.trim() || null,
        default_rule: document.querySelector(`.ag-default[data-agent="${agent}"]`).value,
      };
      const r = await api("/api/mesh/agent", { username: agent, patch });
      if (r.error) toast(r.error, true);
      else toast(`Saved @${agent}`);
    });
  });
  const newAgent = $("#new-agent-btn");
  if (newAgent) newAgent.addEventListener("click", async () => {
    const r = await api("/api/mesh/create_agent", {
      username: $("#new-agent-user").value.trim(),
      display: $("#new-agent-display").value.trim(),
    });
    if (r.error) { toast(r.error, true); return; }
    toast(`Agent @${r.agent.username} created`);
    renderSettings();
  });
  const stPause = $("#st-pause");
  if (stPause) stPause.addEventListener("change", async (e) => {
    const r = await api("/api/mesh/pause", { paused: e.target.checked });
    if (r.error) { toast(r.error, true); return; }
    Mesh.state.paused = r.paused;
    renderChrome();
  });
}
V.renderSettings = renderSettings;
