/* Settings pages — profile, account, chats (theme), my agents, connection.
   The section nav lives in the sidebar. */

import { $, esc, toast, setThemePref, themePref, enterToSend, setEnterToSend,
         ACCENTS, accentPref, setAccent } from "./util.js";
import { ICONS } from "./icons.js";
import { api } from "./api.js";
import { csel, mountCsels } from "./csel.js";
import { openModal, closeModal, swapModal, openPhotoViewer } from "./modal.js";
import { App, Mesh, Settings, RULE_LABELS, meshDn, meshAvatar, meshAvatarInner, renderChrome } from "./state.js";
import { renderSidebar } from "./sidebar.js";
import { V } from "./views.js";

// Theme-picker tile illustrations (task 6): a tiny app mockup per option. The
// surface colours are fixed to each theme so the tile previews that theme even
// when the app is currently in another; the accent bubble uses var(--accent) so
// it also previews the chosen palette colour (task 7). System = a light/dark
// split. No checkbox — the selected tile gets an accent border (.sel).
const THEME_ART = {
  light: `<svg viewBox="0 0 100 64" preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg"><rect width="100" height="64" fill="#FAF9F8"/><rect width="30" height="64" fill="#EFECEA"/><rect x="6" y="9" width="18" height="4" rx="2" fill="#CFCCC9"/><rect x="6" y="17" width="18" height="4" rx="2" fill="#CFCCC9"/><rect x="6" y="25" width="18" height="4" rx="2" fill="#CFCCC9"/><rect x="38" y="11" width="28" height="8" rx="4" fill="#E8E5E3"/><rect x="50" y="27" width="36" height="8" rx="4" fill="var(--accent)"/><rect x="38" y="43" width="22" height="8" rx="4" fill="#E8E5E3"/></svg>`,
  dark: `<svg viewBox="0 0 100 64" preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg"><rect width="100" height="64" fill="#201F1E"/><rect width="30" height="64" fill="#161514"/><rect x="6" y="9" width="18" height="4" rx="2" fill="#3A3937"/><rect x="6" y="17" width="18" height="4" rx="2" fill="#3A3937"/><rect x="6" y="25" width="18" height="4" rx="2" fill="#3A3937"/><rect x="38" y="11" width="28" height="8" rx="4" fill="#2E2D2B"/><rect x="50" y="27" width="36" height="8" rx="4" fill="var(--accent)"/><rect x="38" y="43" width="22" height="8" rx="4" fill="#2E2D2B"/></svg>`,
  system: `<svg viewBox="0 0 100 64" preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg"><rect width="100" height="64" fill="#FAF9F8"/><rect x="50" width="50" height="64" fill="#201F1E"/><rect width="16" height="64" fill="#EFECEA"/><rect x="50" width="16" height="64" fill="#161514"/><rect x="22" y="13" width="22" height="7" rx="3.5" fill="#E8E5E3"/><rect x="20" y="27" width="26" height="7" rx="3.5" fill="var(--accent)"/><rect x="72" y="20" width="22" height="7" rx="3.5" fill="#2E2D2B"/><rect x="70" y="34" width="24" height="7" rx="3.5" fill="var(--accent)"/><line x1="50" y1="0" x2="50" y2="64" stroke="#8A8886" stroke-width="1"/></svg>`,
};

// privacy-matrix rows (audience selects). read_receipts is a toggle, handled
// separately. Order matches WhatsApp's Privacy screen roughly.
const PRIVACY_FIELDS = [
  ["last_seen", "Last seen"],
  ["online", "Online"],
  ["photo", "Profile photo"],
  ["about", "About"],
  ["status", "Status"],
  ["messaging", "Who can message me"],
  ["add_to_group", "Who can add me to groups"],
];
const AUDIENCE_OPTS = [
  { v: "everyone", label: "Everyone" },
  { v: "members", label: "My chats" },
  { v: "nobody", label: "Nobody" },
];

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
  // Profile is merged into Account (task 2); the old #/settings/profile route
  // (bookmarks, etc.) still lands on the merged Account section.
  const section = Settings.section === "profile" ? "account" : (Settings.section || "account");
  const pref = themePref();
  const curAccent = accentPref();
  const back = `<button class="mob-back" onclick="location.hash='#/settings'">${ICONS.back} Settings</button>`;

  // the Account page needs the unfiltered own-account view (about/status/
  // handle/privacy/blocks) — one await, only on this page (not the
  // stutter-sensitive chat switch). v1 has no /api/mesh/me → me stays null and
  // the v2-only surfaces below are simply skipped.
  let me = null;
  if (section === "account") {
    const r = await api("/api/mesh/me");
    if (!r.error) me = r;
  }

  let html = "";
  if (section === "account") {
    const hasPhoto = !!meshAvatar(ms.user);
    const handle = me?.handle || ms.user;
    const about = me?.about || "";
    const status = me?.status || { state: "available", text: "" };
    html = `${back}<h1>Account</h1>
      <div class="card">
        <div class="pf-photo-wrap">
          <div class="pf-photo">${meshAvatarInner(ms.user)}</div>
          <button class="pf-edit" id="pf-edit">${ICONS.camera} Edit</button>
          <div class="menu pf-menu" id="pf-menu" hidden>
            ${hasPhoto ? `<button data-act="view">${ICONS.eye} View photo</button>` : ""}
            <button data-act="camera">${ICONS.camera} ${hasPhoto ? "Retake photo" : "Take photo"}</button>
            <button data-act="upload">${ICONS.media} Upload photo</button>
            ${hasPhoto ? `<button class="danger-item" data-act="remove">${ICONS.trash} Remove photo</button>` : ""}
          </div>
        </div>
        <div class="acct-identity">
          <div class="acct-name-line">
            <span id="acct-name" class="acct-name">${esc(meshDn(ms.user))}</span>
            <button class="acct-name-edit" id="acct-name-edit" aria-label="Edit name">${ICONS.pencil}</button>
          </div>
          <div class="hint">member</div>
        </div>
      </div>
      ${me ? `
      <div class="card">
        <h2>Profile</h2>
        <dl class="kv" style="grid-template-columns:minmax(90px,120px) 1fr">
          <dt>Username</dt>
          <dd><span class="acct-handle-line"><span id="acct-handle">@${esc(handle)}</span>
            <button class="icon-btn ci-pencil" id="acct-handle-edit" aria-label="Change username">${ICONS.pencil}</button></span></dd>
          <dt>About</dt>
          <dd><span class="acct-about-line"><span id="acct-about">${about ? esc(about) : '<span class="hint">Add a few words about you</span>'}</span>
            <button class="icon-btn ci-pencil" id="acct-about-edit" aria-label="Edit about">${ICONS.pencil}</button></span></dd>
          <dt>Status</dt>
          <dd><span class="csel-slot acct-status-state" data-value="${esc(status.state)}"></span>
            <input type="text" id="acct-status-text" placeholder="What's happening?" value="${esc(status.text)}" maxlength="80" style="margin-top:6px;width:100%">
            <button class="primary" id="acct-status-save" style="margin-top:6px">Save status</button></dd>
        </dl>
      </div>
      <div class="card" id="privacy-card">
        <h2>Privacy</h2>
        <dl class="kv" style="grid-template-columns:minmax(120px,180px) 1fr">
          ${PRIVACY_FIELDS.map(([k, label]) =>
            `<dt>${label}</dt><dd><span class="csel-slot pv-aud" data-field="${k}" data-value="${esc(me.privacy?.[k] || "everyone")}"></span></dd>`).join("")}
        </dl>
        <div class="row" style="margin-top:6px"><label class="switch">
          <input type="checkbox" id="pv-read-receipts" ${me.privacy?.read_receipts !== false ? "checked" : ""}>
          <span class="slider"></span></label>
          <span><b>Read receipts</b> — send and see the blue ticks</span></div>
        <p class="hint" style="margin-bottom:0">"Members" means people you share a
        chat with. Turning read receipts off hides them both ways.</p>
      </div>
      <div class="card">
        <h2>Security</h2>
        <div class="row"><button id="acct-password">Change password</button></div>
        <p class="hint" style="margin-bottom:0">Your messages are end-to-end
        encrypted. Your password unlocks your keys on each device; changing it
        re-wraps them and keeps your recovery code working.</p>
      </div>` : ""}
      <div class="card">
        <h2>Session</h2>
        <div class="row"><button id="st-logout">Sign out</button></div>
        <p class="hint" style="margin-bottom:0">Your account lives in the shared
        folder — it works from any machine that syncs it, and your photo, name and
        settings follow you.</p>
      </div>
      <input type="file" id="pf-file" accept="image/*" hidden>`;
  } else if (section === "chats") {
    html = `${back}<h1>Chats</h1>
      <div class="card">
        <h2>Appearance</h2>
        <div class="theme-tiles">
          ${["system", "light", "dark"].map((p) => `
            <button class="theme-tile ${pref === p ? "sel" : ""}" data-theme-pref="${p}">
              <span class="tt-art">${THEME_ART[p]}</span>
              <span class="tt-label">${p[0].toUpperCase() + p.slice(1)}</span>
            </button>`).join("")}
        </div>
        <p class="hint" style="margin-bottom:0">System follows your device's
        light/dark setting.</p>
        <div style="margin-top:18px">
          <div style="font-weight:600;font-size:12.5px;margin-bottom:10px">Accent color</div>
          <div class="accent-dots">
            ${ACCENTS.map((a) => `<button class="accent-dot ${curAccent === a.id ? "sel" : ""}" data-accent-id="${a.id}" title="${esc(a.label)}" style="--dot:${a.hex}"></button>`).join("")}
          </div>
        </div>
      </div>
      <div class="card">
        <h2>Messaging</h2>
        <div class="row">
          <label class="switch">
            <input type="checkbox" id="enter-send" ${enterToSend() ? "checked" : ""}>
            <span class="slider"></span>
          </label>
          <span><b>Enter to send</b> — press Enter to send; Shift+Enter for a new line</span>
        </div>
        <p class="hint" style="margin-bottom:0">Turn this off to send with
        Ctrl+Enter and use Enter for new lines. This device only.</p>
      </div>`;
  } else if (section === "agents") {
    const mine = Object.values(ms.users)
      .filter((u) => u.kind === "agent" && (u.owners || []).includes(ms.user));
    // per-purpose routing rows: who the agent replies TO decides the model,
    // and whether that audience is served at all (the enable switch)
    const CATS = [["owner", "You"], ["humans", "Other people"], ["agents", "Agents"]];
    const routeRow = (a, st) => ([cat, label]) => {
      const r = (st.routing || {})[cat] || {};
      return `<div class="ag-route">
          <label class="switch"><input type="checkbox" class="ag-route-on"
            data-agent="${esc(a.username)}" data-cat="${cat}"
            ${r.enabled === false ? "" : "checked"}>
            <span class="slider"></span></label>
          <span class="ag-route-name">${label}</span>
          <span class="csel-slot ag-route-model" data-agent="${esc(a.username)}"
            data-cat="${cat}" data-value="${esc(r.model || "")}"></span>
        </div>`;
    };
    html = `${back}<h1>My agents</h1>
      ${mine.map((a) => {
        const st = a.settings || {};
        return `
        <div class="card">
          <div class="ag-head">
            <div class="ag-avatar-wrap">
              <span class="ag-avatar">${meshAvatarInner(a.username)}</span>
              <button class="ci-cam ag-cam" data-agent="${esc(a.username)}"
                aria-label="Change ${esc(a.display)} photo">${ICONS.camera}</button>
              <div class="menu ci-photo-menu ag-photo-menu" data-agent="${esc(a.username)}" hidden>
                ${a.avatar ? `<button data-act="view">${ICONS.eye} View photo</button>` : ""}
                <button data-act="camera">${ICONS.camera} ${a.avatar ? "Retake photo" : "Take photo"}</button>
                <button data-act="upload">${ICONS.media} Upload photo</button>
                ${a.avatar ? `<button class="danger-item" data-act="remove">${ICONS.trash} Remove photo</button>` : ""}
              </div>
            </div>
            <h2 style="margin:0">${esc(a.display)} <span class="hint" style="text-transform:none">@${esc(a.username)}</span></h2>
          </div>
          <dl class="kv" style="grid-template-columns:minmax(110px,160px) 1fr">
            <dt>Runs with</dt><dd><span class="csel-slot ag-adapter"
              data-agent="${esc(a.username)}" data-value="${esc(st.adapter || "")}"></span></dd>
            <dt>Current model</dt><dd><span class="csel-slot ag-model"
              data-agent="${esc(a.username)}" data-value="${esc(st.model || "")}"></span></dd>
            <dt>Reasoning effort</dt><dd><span class="csel-slot ag-reason"
              data-agent="${esc(a.username)}" data-value="${esc(st.reasoning || "")}"></span></dd>
            <dt>Default reply rule</dt><dd><span class="csel-slot ag-default"
              data-agent="${esc(a.username)}" data-value="${esc(st.default_rule || "tagged")}"></span></dd>
            <dt>Replies per hour</dt><dd><span class="csel-slot ag-rate"
              data-agent="${esc(a.username)}"
              data-value="${st.max_replies_per_hour != null ? esc(st.max_replies_per_hour) : ""}"></span></dd>
            <dt>Replies to</dt><dd class="ag-routes" data-agent="${esc(a.username)}">
              ${CATS.map(routeRow(a, st)).join("")}</dd>
            <dt>Runs on</dt><dd class="ag-machine" data-agent="${esc(a.username)}">
              <span class="mono">${esc(a.machine || "unknown")}</span></dd>
            <dt>Owner</dt><dd>${(a.owners || []).map((o) => esc("@" + o)).join(", ")}</dd>
          </dl>
          <p class="hint">"Current model" applies everywhere; the per-audience
          models below kick in when it's left on the family default. Per-chat
          rules live in each chat's info page.</p>
          <div class="row"><button class="primary ag-save" data-agent="${esc(a.username)}">Save</button></div>
        </div>`;
      }).join("") || ""}
      <div class="card">
        <h2>Add an agent</h2>
        <div class="row">
          <input type="text" id="new-agent-user" placeholder="username (e.g. coco2)">
          <input type="text" id="new-agent-display" placeholder="Display name">
          <button class="primary" id="new-agent-btn">Create</button>
        </div>
        <p class="hint" style="margin-bottom:0">You become its responsible member;
        it runs on this machine's harness.</p>
      </div>
      <div class="card">
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
      <div class="card">
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
      <div class="card">
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

  // theme tiles: click to pick System / Light / Dark; setThemePref applies it
  // live (data-theme flips → the whole app re-themes) and the accent bubble in
  // each tile follows suit. No re-render needed — just move the .sel border.
  document.querySelectorAll(".theme-tile").forEach((t) => {
    t.addEventListener("click", () => {
      setThemePref(t.dataset.themePref);
      document.querySelectorAll(".theme-tile").forEach((x) => x.classList.toggle("sel", x === t));
    });
  });
  // accent dots: pick a colour → setAccent applies it live everywhere (the theme
  // tiles' accent bubbles follow too, since they use var(--accent)).
  document.querySelectorAll(".accent-dot").forEach((d) => {
    d.addEventListener("click", () => {
      setAccent(d.dataset.accentId);
      document.querySelectorAll(".accent-dot").forEach((x) => x.classList.toggle("sel", x === d));
    });
  });
  const enterSend = $("#enter-send");
  if (enterSend) enterSend.addEventListener("change", (e) => setEnterToSend(e.target.checked));
  const logout = $("#st-logout");
  if (logout) logout.addEventListener("click", async () => {
    await api("/api/mesh/logout", {});
    location.hash = "#/chats";
  });
  // edit display name inline (username is fixed — the identity key). The ✎ swaps
  // the name line for an input + Save/Cancel; Enter saves, Escape cancels.
  const nameEdit = $("#acct-name-edit");
  if (nameEdit) nameEdit.addEventListener("click", () => {
    const line = $(".acct-name-line");
    const cur = meshDn(ms.user);
    line.innerHTML = `<input type="text" id="acct-name-input" maxlength="64" value="${esc(cur)}">
      <button class="primary" id="acct-name-save">Save</button>
      <button id="acct-name-cancel">Cancel</button>`;
    const inp = $("#acct-name-input"); inp.focus(); inp.select();
    const cancel = () => renderSettings();
    const save = async () => {
      const v = inp.value.trim();
      if (!v) { toast("Name can't be empty", true); return; }
      if (v === cur) return cancel();
      const r = await api("/api/mesh/set_display", { display: v });
      if (r.error) { toast(r.error, true); return; }
      const u = Mesh.state?.users?.[ms.user];
      if (u) u.display = r.display;   // show it now, don't wait for the poll
      toast("Name updated", { check: true });
      renderSettings(); renderSidebar();
    };
    $("#acct-name-save").addEventListener("click", save);
    $("#acct-name-cancel").addEventListener("click", cancel);
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); save(); }
      else if (e.key === "Escape") cancel();
    });
  });
  wireAccountEditors(ms);
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
  // My-agents dropdowns use the SAME custom select as Performance (task 13):
  // reply-rule (was a native <select>) and replies-per-hour (was a number input
  // whose spinner + datalist arrow read as a doubled dropdown). mountCsels fills
  // each .csel-slot from its data-value and writes the choice back to it.
  // mountCsels fills EVERY .csel-slot with the agent rule/rate options, so it
  // must run ONLY on the agents page — the account page has its own .csel-slot
  // elements (privacy audiences, status) mounted by wireAccountEditors.
  if (section === "agents") {
    const ruleOpts = Object.entries(RULE_LABELS).map(([v, label]) => ({ v, label }));
    const rateOpts = [
      { v: "", label: "Default (30 / hour)" },
      ...[10, 20, 30, 50, 100, 200, 500].map((n) => ({ v: String(n), label: `${n} / hour` })),
    ];
    // the picker's options come from THIS machine's installed CLI families
    // (R16): fetch them, then mount every dropdown in one pass. A family with
    // no model list degrades to enable/disable only (disabled model selects).
    (async () => {
      const ho = await api("/api/mesh/harness_options");
      const FAMS = ho.families || [];
      const avail = FAMS.filter((f) => f.available);
      const famById = (id) => FAMS.find((f) => f.id === id);
      const famFor = (agent) => {
        const slot = document.querySelector(`.ag-adapter[data-agent="${agent}"]`);
        return famById(slot?.dataset.value)
          || (avail.length === 1 ? avail[0] : null);
      };
      const adapterOpts = [
        { v: "", label: avail.length === 1 ? `Auto — ${avail[0].label}`
            : avail.length ? "Pick a CLI…" : "No agent CLI installed" },
        ...FAMS.map((f) => ({
          v: f.id, label: f.label + (f.available ? "" : " (not installed)") })),
      ];
      const modelOpts = (fam, blank) => [
        { v: "", label: blank },
        ...((fam && fam.models) || []).map((m) => ({ v: m, label: m })),
      ];
      const effortOpts = (fam) => [
        { v: "", label: "Default" },
        ...((fam && fam.efforts) || []).map((e) => ({ v: e, label: e })),
      ];
      const remount = (slot, options, disabled) => {
        slot.innerHTML = "";
        slot.appendChild(csel({ options, value: slot.dataset.value || "",
          disabled, onChange: (v) => { slot.dataset.value = v; } }));
      };
      const refreshModels = (agent) => {
        const fam = famFor(agent);
        const noModels = !((fam && fam.models) || []).length;
        document.querySelectorAll(`.ag-model[data-agent="${agent}"]`)
          .forEach((s) => remount(s, modelOpts(fam, "Family default"), noModels));
        document.querySelectorAll(`.ag-reason[data-agent="${agent}"]`)
          .forEach((s) => remount(s, effortOpts(fam),
                                  !((fam && fam.efforts) || []).length));
        document.querySelectorAll(`.ag-route-model[data-agent="${agent}"]`)
          .forEach((s) => remount(s, modelOpts(fam, "Use current model"), noModels));
      };
      mountCsels($(".settings-body"), (slot) => {
        if (slot.classList.contains("ag-adapter")) return adapterOpts;
        if (slot.classList.contains("ag-model"))
          return modelOpts(famFor(slot.dataset.agent), "Family default");
        if (slot.classList.contains("ag-reason"))
          return effortOpts(famFor(slot.dataset.agent));
        if (slot.classList.contains("ag-route-model"))
          return modelOpts(famFor(slot.dataset.agent), "Use current model");
        if (!slot.classList.contains("ag-rate")) return ruleOpts;
        // surface a previously-set non-preset value so it labels correctly
        const cur = slot.dataset.value;
        return cur && !rateOpts.some((o) => o.v === cur)
          ? [rateOpts[0], { v: cur, label: `${cur} / hour` }, ...rateOpts.slice(1)]
          : rateOpts;
      }, (slot, v) => {
        if (slot.classList.contains("ag-adapter")) {
          // a family switch orphans the old family's model picks — clear them
          document.querySelectorAll(
            `.ag-model[data-agent="${slot.dataset.agent}"],
             .ag-reason[data-agent="${slot.dataset.agent}"],
             .ag-route-model[data-agent="${slot.dataset.agent}"]`)
            .forEach((s) => { s.dataset.value = ""; });
          refreshModels(slot.dataset.agent);
        }
      });
      // apply the degrade rules to the initial mount too
      document.querySelectorAll(".ag-adapter").forEach(
        (s) => refreshModels(s.dataset.agent));
      // an agent homed on another machine gets a one-click adoption (the
      // owner-side bring-up path for migrated agents)
      document.querySelectorAll(".ag-machine").forEach((dd) => {
        const agent = dd.dataset.agent;
        const rec = (Mesh.state.users || {})[agent] || {};
        if (!rec.machine || !ho.machine || rec.machine === ho.machine) return;
        const b = document.createElement("button");
        b.textContent = "Adopt to this machine";
        b.style.marginLeft = "10px";
        b.addEventListener("click", async () => {
          const r = await api("/api/mesh/adopt_agent", { username: agent });
          if (r.error) { toast(r.error, true); return; }
          toast(`@${agent} now runs on this machine`);
          await V.refreshState?.();
          renderSettings();
        });
        dd.appendChild(b);
      });
    })();
  }
  document.querySelectorAll(".ag-save").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const agent = btn.dataset.agent;
      const val = (sel) =>
        document.querySelector(`${sel}[data-agent="${agent}"]`)?.dataset.value || "";
      const rateRaw = val(".ag-rate");
      const rateN = parseInt(rateRaw, 10);
      const routing = {};
      document.querySelectorAll(`.ag-route-on[data-agent="${agent}"]`)
        .forEach((sw) => {
          routing[sw.dataset.cat] = {
            enabled: sw.checked,
            model: document.querySelector(
              `.ag-route-model[data-agent="${agent}"][data-cat="${sw.dataset.cat}"]`
            )?.dataset.value || "",
          };
        });
      const patch = {
        adapter: val(".ag-adapter") || null,
        model: val(".ag-model") || null,
        reasoning: val(".ag-reason") || null,
        default_rule: val(".ag-default"),
        routing,
        // blank clears back to the default; otherwise clamp to a sane band
        max_replies_per_hour: !rateRaw || isNaN(rateN)
          ? null : Math.max(1, Math.min(1000, rateN)),
      };
      const r = await api("/api/mesh/agent", { username: agent, patch });
      if (r.error) toast(r.error, true);
      else toast(`Saved @${agent}`);
    });
  });
  // owner: set/clear each agent's photo — Take / Upload / Remove via the shared
  // V.photoCamera / V.photoPickFile capture flows (same as Group Info)
  document.querySelectorAll(".ag-cam").forEach((cam) => {
    const menu = cam.closest(".ag-avatar-wrap").querySelector(".ag-photo-menu");
    const agent = cam.dataset.agent;
    cam.addEventListener("click", (e) => {
      e.stopPropagation();
      const opening = menu.hidden;
      document.querySelectorAll(".ag-photo-menu").forEach((m) => { if (m !== menu) m.hidden = true; });
      menu.hidden = !opening;
      if (opening) {   // dismiss on the next outside click
        const closer = (ev) => {
          if (!menu.contains(ev.target) && !cam.contains(ev.target)) {
            menu.hidden = true;
            document.removeEventListener("mousedown", closer);
          }
        };
        setTimeout(() => document.addEventListener("mousedown", closer), 0);
      }
    });
    menu.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
      menu.hidden = true;
      const onBlob = (blob) => uploadAgentAvatar(agent, blob);
      if (b.dataset.act === "view") {
        const av = cam.closest(".ag-avatar-wrap").querySelector(".ag-avatar");
        const img = av?.querySelector(".avatar-img");
        if (img) openPhotoViewer(img.src, meshDn(agent), av);
      }
      else if (b.dataset.act === "camera") V.photoCamera(onBlob);
      else if (b.dataset.act === "upload") V.photoPickFile(onBlob);
      else if (b.dataset.act === "remove") clearAgentAvatar(agent);
    }));
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

  // ---- profile photo: Edit → menu (Upload photo / Remove) ----
  const pfEdit = $("#pf-edit");
  if (pfEdit) {
    const menu = $("#pf-menu");
    const file = $("#pf-file");
    pfEdit.addEventListener("click", (e) => {
      e.stopPropagation();
      const opening = menu.hidden;
      menu.hidden = !opening;
      if (opening) {   // dismiss on the next outside click
        const closer = (ev) => {
          if (!menu.contains(ev.target) && !pfEdit.contains(ev.target)) {
            menu.hidden = true;
            document.removeEventListener("mousedown", closer);
          }
        };
        setTimeout(() => document.addEventListener("mousedown", closer), 0);
      }
    });
    // in Settings the viewer opens from the "View photo" menu item (NOT by
    // clicking the pic — user's task 7), flying from the .pf-photo disc
    const pf = $(".pf-photo");
    const viewPhoto = () => {
      const img = pf?.querySelector(".avatar-img");
      if (img) openPhotoViewer(img.src, meshDn(ms.user), pf);
    };
    menu.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
      menu.hidden = true;
      if (b.dataset.act === "view") viewPhoto();
      else if (b.dataset.act === "camera") openCamera(uploadAvatar);
      else if (b.dataset.act === "upload") file.click();
      else if (b.dataset.act === "remove") removeAvatar();
    }));
    file.addEventListener("change", () => {
      const f = file.files && file.files[0];
      file.value = "";   // allow re-picking the same file
      if (f) openAvatarAdjust(f, uploadAvatar);
    });
  }
}
V.renderSettings = renderSettings;

// ---- account editors (v2): @handle, about, status, privacy, password --------
function wireAccountEditors(ms) {
  // @handle change — inline edit, like the display name (name is the immutable
  // identity; the handle is the mutable @-mention, Telegram model)
  const hEdit = $("#acct-handle-edit");
  if (hEdit) hEdit.addEventListener("click", () => {
    const line = $(".acct-handle-line");
    const cur = ($("#acct-handle").textContent || "").replace(/^@/, "");
    line.innerHTML = `<input type="text" id="acct-handle-input" maxlength="32" value="${esc(cur)}">
      <button class="primary" id="acct-handle-save">Save</button>
      <button id="acct-handle-cancel">Cancel</button>`;
    const inp = $("#acct-handle-input"); inp.focus(); inp.select();
    const save = async () => {
      const v = inp.value.trim().toLowerCase().replace(/^@/, "");
      if (!v || v === cur) return renderSettings();
      const r = await api("/api/mesh/set_handle", { handle: v });
      if (r.error) { toast(r.error, true); return; }
      toast("Username updated", { check: true }); renderSettings();
    };
    $("#acct-handle-save").addEventListener("click", save);
    $("#acct-handle-cancel").addEventListener("click", () => renderSettings());
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); save(); }
      else if (e.key === "Escape") renderSettings();
    });
  });

  // About — inline edit
  const aEdit = $("#acct-about-edit");
  if (aEdit) aEdit.addEventListener("click", () => {
    const line = $(".acct-about-line");
    const cur = $("#acct-about").textContent.trim();
    const seed = cur === "Add a few words about you" ? "" : cur;
    line.innerHTML = `<input type="text" id="acct-about-input" maxlength="139" value="${esc(seed)}">
      <button class="primary" id="acct-about-save">Save</button>
      <button id="acct-about-cancel">Cancel</button>`;
    const inp = $("#acct-about-input"); inp.focus();
    const save = async () => {
      const r = await api("/api/mesh/set_about", { about: inp.value.trim() });
      if (r.error) { toast(r.error, true); return; }
      toast("About updated", { check: true }); renderSettings();
    };
    $("#acct-about-save").addEventListener("click", save);
    $("#acct-about-cancel").addEventListener("click", () => renderSettings());
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); save(); }
      else if (e.key === "Escape") renderSettings();
    });
  });

  // Status — a state csel + a free-text line, saved together
  const statusSlot = document.querySelector(".acct-status-state");
  if (statusSlot) statusSlot.appendChild(csel({
    options: [
      { v: "available", label: "🟢 Available" },
      { v: "busy", label: "🟠 Busy" },
      { v: "dnd", label: "⛔ Do not disturb" },
      { v: "away", label: "🌙 Away" },
    ],
    value: statusSlot.dataset.value || "available",
    onChange: (v) => { statusSlot.dataset.value = v; },
  }));
  const stSave = $("#acct-status-save");
  if (stSave) stSave.addEventListener("click", async () => {
    const r = await api("/api/mesh/set_status", {
      state: statusSlot.dataset.value || "available",
      text: ($("#acct-status-text").value || "").trim(),
    });
    if (r.error) { toast(r.error, true); return; }
    toast("Status updated", { check: true });
  });

  // Privacy matrix — one audience csel per field; change POSTs immediately
  document.querySelectorAll(".pv-aud").forEach((slot) => {
    slot.appendChild(csel({
      options: AUDIENCE_OPTS,
      value: slot.dataset.value || "everyone",
      onChange: async (v) => {
        const r = await api("/api/mesh/set_privacy",
          { privacy: { [slot.dataset.field]: v } });
        if (r.error) toast(r.error, true);
      },
    }));
  });
  const rr = $("#pv-read-receipts");
  if (rr) rr.addEventListener("change", async (e) => {
    // one toggle drives both directions (send + view) — WhatsApp semantics
    const on = e.target.checked;
    const r = await api("/api/mesh/set_privacy",
      { privacy: { read_receipts: on, view_read_receipts: on } });
    if (r.error) toast(r.error, true);
  });

  // Change password — a small modal (current + new + confirm)
  const pw = $("#acct-password");
  if (pw) pw.addEventListener("click", () => openPasswordModal());
}

function openPasswordModal() {
  const box = openModal(`
    <div class="cf-title">Change password</div>
    <dl class="kv" style="grid-template-columns:120px 1fr;margin:12px 0">
      <dt>Current</dt><dd><input type="password" id="pw-old" autocomplete="current-password"></dd>
      <dt>New</dt><dd><input type="password" id="pw-new" autocomplete="new-password"></dd>
      <dt>Confirm</dt><dd><input type="password" id="pw-new2" autocomplete="new-password"></dd>
    </dl>
    <div class="cf-actions">
      <button class="cf-cancel" id="pw-cancel">Cancel</button>
      <button class="cf-pill" id="pw-go">Change</button>
    </div>`);
  box.classList.add("confirm");
  box.parentElement.classList.add("confirm-scrim");
  box.querySelector("#pw-cancel").addEventListener("click", closeModal);
  box.querySelector("#pw-go").addEventListener("click", async () => {
    const oldp = box.querySelector("#pw-old").value;
    const newp = box.querySelector("#pw-new").value;
    const conf = box.querySelector("#pw-new2").value;
    if (newp.length < 6) { toast("New password must be at least 6 characters", true); return; }
    if (newp !== conf) { toast("The new passwords don't match", true); return; }
    const r = await api("/api/mesh/change_password", { old: oldp, new: newp });
    if (r.error) { toast(r.error, true); return; }
    closeModal(); toast("Password changed", { check: true });
  });
  box.querySelector("#pw-old").focus();
}

// ---- profile photo: upload → adjust (crop/zoom in a circle) → downsize -------

function openAvatarAdjust(file, onBlob) {
  const url = URL.createObjectURL(file);
  const img = new Image();
  img.onload = () => { URL.revokeObjectURL(url); mountAvatarAdjuster(img, onBlob); };
  img.onerror = () => { URL.revokeObjectURL(url); toast("Couldn't read that image", true); };
  img.src = url;
}

// ---- profile photo: take a photo with the device camera (Round B) -----------
// getUserMedia needs a secure context — fine on 127.0.0.1; a LAN-over-http phone
// won't get camera access until the app serves HTTPS (a later feature).
async function openCamera(onBlob) {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    toast("The camera isn't available here", true);
    return;
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "user", width: { ideal: 1280 }, height: { ideal: 1280 } },
      audio: false,
    });
  } catch (e) {
    const name = e && e.name;
    toast(
      name === "NotAllowedError" || name === "SecurityError"
        ? "Camera access was blocked — allow the camera in your browser, then try again."
      : name === "NotFoundError" || name === "OverconstrainedError" || name === "DevicesNotFoundError"
        ? "No camera found on this device."
      : name === "NotReadableError" || name === "TrackStartError"
        ? "The camera is already in use by another app."
        : "Couldn't open the camera.",
      true);
    return;
  }
  mountCamera(stream, onBlob);
}

// Live viewfinder in the same illuminated-circle stage; the shutter grabs the
// current frame (mirrored, to match the selfie preview) and hands it to the
// SAME crop/zoom adjuster the upload path uses. The camera stream is stopped on
// every close path so the webcam light never lingers.
function mountCamera(stream, onBlob) {
  const box = openModal(`
    <div class="ava-adjust cam-shoot">
      <div class="ava-adjust-head">
        <button class="icon-btn" id="cam-cancel" aria-label="Cancel">${ICONS.close}</button>
        <span>Take photo</span>
      </div>
      <div class="ava-stage">
        <video id="cam-video" autoplay playsinline muted></video>
        <div class="ava-mask"></div>
      </div>
      <button class="ava-confirm primary" id="cam-shot" aria-label="Capture photo">${ICONS.camera}</button>
    </div>`);
  box.classList.add("ava-modal");
  const video = box.querySelector("#cam-video");
  video.srcObject = stream;
  video.play().catch(() => {});
  const stop = () => stream.getTracks().forEach((t) => t.stop());
  // stop the camera on ANY close (cancel, capture, or an outside-click that
  // removes the modal scrim) — otherwise the stream (and webcam light) lingers
  const obs = new MutationObserver(() => {
    if (!document.body.contains(video)) { stop(); obs.disconnect(); }
  });
  obs.observe(document.body, { childList: true, subtree: true });
  box.querySelector("#cam-cancel").addEventListener("click", () => { stop(); closeModal(); });
  box.querySelector("#cam-shot").addEventListener("click", () => {
    const vw = video.videoWidth, vh = video.videoHeight;
    if (!vw || !vh) { toast("The camera is still starting — try again", true); return; }
    // Capture the FULL frame (mirrored to match the preview). The viewfinder
    // shows the whole photo uncropped — no crop circle — so what you see is
    // exactly what's captured; the user then pans/zooms into the circle in the
    // adjuster step, and nothing moves between the two (2026-07-11).
    const cap = document.createElement("canvas");
    cap.width = vw; cap.height = vh;
    const cctx = cap.getContext("2d");
    cctx.translate(vw, 0); cctx.scale(-1, 1);   // mirror to match the preview
    cctx.drawImage(video, 0, 0, vw, vh);
    stop(); obs.disconnect();
    // reuse the open camera modal (swapModal, inside mountAvatarAdjuster) so
    // the viewfinder → crop step has no scrim flash and stays the same size
    mountAvatarAdjuster(cap, onBlob);   // a canvas is a valid source
  });
}

// A fixed 300px stage with a centred 260px crop circle. Accepts an <img> (from
// an upload) OR a <canvas> (a captured camera frame). The source pans/zooms but
// is clamped so the circle is always fully covered; on confirm the circle's
// bounding square is drawn into a 512×512 export canvas and exported as JPEG —
// downsized entirely client-side (the backend keeps no image library).
function mountAvatarAdjuster(img, onBlob) {
  const S = 300, D = 260, c = S / 2, cropL = c - D / 2, cropT = c - D / 2;
  const box = swapModal(`
    <div class="ava-adjust">
      <div class="ava-adjust-head">
        <button class="icon-btn" id="ava-cancel" aria-label="Cancel">${ICONS.close}</button>
        <span>Drag to reposition</span>
      </div>
      <div class="ava-stage">
        <canvas id="ava-canvas" width="${S}" height="${S}"></canvas>
        <div class="ava-mask"></div>
      </div>
      <div class="ava-zoom">
        <span class="ava-zi">&minus;</span>
        <input type="range" id="ava-zoom" min="1" max="4" step="0.005" value="1">
        <span class="ava-zi">+</span>
      </div>
      <button class="ava-confirm primary" id="ava-confirm" aria-label="Set photo">${ICONS.check}</button>
    </div>`);
  box.classList.add("ava-modal");
  const canvas = box.querySelector("#ava-canvas");
  const ctx = canvas.getContext("2d");
  const slider = box.querySelector("#ava-zoom");
  const minScale = D / Math.min(img.width, img.height);
  let zoom = 1, scale = minScale;
  let ox = (S - img.width * scale) / 2, oy = (S - img.height * scale) / 2;
  const clamp = () => {
    const w = img.width * scale, h = img.height * scale;
    ox = Math.min(cropL, Math.max(cropL + D - w, ox));
    oy = Math.min(cropT, Math.max(cropT + D - h, oy));
  };
  const draw = () => {
    ctx.clearRect(0, 0, S, S);
    ctx.drawImage(img, ox, oy, img.width * scale, img.height * scale);
  };
  clamp(); draw();
  let drag = null;
  canvas.addEventListener("pointerdown", (e) => {
    drag = { x: e.clientX, y: e.clientY, ox, oy };
    canvas.setPointerCapture(e.pointerId);
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!drag) return;
    ox = drag.ox + (e.clientX - drag.x);
    oy = drag.oy + (e.clientY - drag.y);
    clamp(); draw();
  });
  const endDrag = () => { drag = null; };
  canvas.addEventListener("pointerup", endDrag);
  canvas.addEventListener("pointercancel", endDrag);
  const zoomTo = (nz) => {
    nz = Math.max(1, Math.min(4, nz));
    const ns = minScale * nz;
    ox = c - (c - ox) * (ns / scale);   // zoom about the circle centre
    oy = c - (c - oy) * (ns / scale);
    zoom = nz; scale = ns;
    clamp(); draw();
    slider.value = String(nz);
  };
  slider.addEventListener("input", () => zoomTo(parseFloat(slider.value)));
  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    zoomTo(zoom * (e.deltaY < 0 ? 1.06 : 0.94));
  }, { passive: false });
  box.querySelector("#ava-cancel").addEventListener("click", closeModal);
  box.querySelector("#ava-confirm").addEventListener("click", () => {
    const out = document.createElement("canvas");
    out.width = out.height = 512;
    const sx = (cropL - ox) / scale, sy = (cropT - oy) / scale, sz = D / scale;
    out.getContext("2d").drawImage(img, sx, sy, sz, sz, 0, 0, 512, 512);
    out.toBlob((blob) => onBlob(blob), "image/jpeg", 0.85);
    // close the cropper right away — the upload runs in the background. Done
    // here (not in each onBlob) so EVERY path closes: the group/agent uploaders
    // didn't, so their ✓ committed the photo but left the popup open (task 10).
    closeModal();
  });
}

// Reusable capture flows for other views (group photo lives in details.js).
// Each hands the finished 512px JPEG blob to onBlob; the crop/zoom adjuster
// and camera viewfinder are shared, so they behave identically everywhere.
V.photoPickFile = (onBlob) => {
  const inp = document.createElement("input");
  inp.type = "file";
  inp.accept = "image/*";
  inp.addEventListener("change", () => {
    const f = inp.files && inp.files[0];
    if (f) openAvatarAdjust(f, onBlob);
  });
  inp.click();
};
V.photoCamera = (onBlob) => openCamera(onBlob);

async function uploadAvatar(blob) {
  if (!blob) { toast("Couldn't process that image", true); return; }
  // the crop adjuster closes itself on ✓ now (task 10) — no closeModal here
  toast("Setting profile image", { spinner: true });
  try {
    const r = await fetch("/api/mesh/set_avatar", { method: "POST", body: blob });
    const j = await r.json();
    if (j.error) { toast(j.error, { error: true, swap: true }); return; }
    const u = Mesh.state?.users?.[Mesh.state.user];   // show it now, don't wait for the poll
    if (u) u.avatar = j.avatar;
    toast("Profile image set", { check: true, swap: true });
    renderSettings();
    renderSidebar();
  } catch (e) {
    toast("Couldn't set the photo — try again", { error: true, swap: true });
  }
}

async function removeAvatar() {
  const r = await api("/api/mesh/clear_avatar", {});
  if (r.error) { toast(r.error, true); return; }
  const u = Mesh.state?.users?.[Mesh.state.user];
  if (u) delete u.avatar;
  toast("Profile photo removed", { check: true });
  renderSettings();
  renderSidebar();
}

// Owner-set an agent's photo (agents can't sign in, so their responsible human
// sets it). Same transport as the group photo; the agent rides the query.
async function uploadAgentAvatar(agent, blob) {
  if (!blob) { toast("Couldn't process that image", true); return; }
  toast("Setting agent photo", { spinner: true });
  try {
    const r = await fetch(`/api/mesh/set_agent_avatar?agent=${encodeURIComponent(agent)}`,
                          { method: "POST", body: blob });
    const j = await r.json();
    if (j.error) { toast(j.error, { error: true, swap: true }); return; }
    const u = Mesh.state?.users?.[agent];   // show it now, don't wait for the poll
    if (u) u.avatar = j.avatar;
    toast("Agent photo set", { check: true, swap: true });
    renderSettings();
    renderSidebar();
  } catch (e) {
    toast("Couldn't set the photo — try again", { error: true, swap: true });
  }
}

async function clearAgentAvatar(agent) {
  const r = await api("/api/mesh/clear_agent_avatar", { agent });
  if (r.error) { toast(r.error, true); return; }
  const u = Mesh.state?.users?.[agent];
  if (u) delete u.avatar;
  toast("Agent photo removed", { check: true });
  renderSettings();
  renderSidebar();
}
