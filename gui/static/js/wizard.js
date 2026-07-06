/* Setup wizard — legacy-shaped (bridge-era onboarding); the setup overhaul
   replaces this. Hidden from the UI; reachable while unconfigured. */

import { $, esc, dn, toast } from "./util.js";
import { api } from "./api.js";
import { App, freshWizard } from "./state.js";
import { V } from "./views.js";

const WIZ_STEPS = ["Welcome", "Install location", "System checks", "Shared folder",
                   "Identity", "Create bridge", "Claude skills", "Remote agent", "Finish"];

function renderSetup() {
  const w = App.wizard;
  const stepsHtml = WIZ_STEPS.map((name, i) => `
    <li class="${i === w.step ? "current" : i < w.step ? "done" : ""}">
      <span class="n">${i < w.step ? "✓" : i + 1}</span> ${name}
    </li>`).join("");
  $("#content").innerHTML = `
    <h1>Setup</h1>
    <p class="page-sub">Guided onboarding — from nothing to a working bridge.</p>
    <div id="wizard">
      <div id="wizard-steps" class="card"><ol>${stepsHtml}</ol></div>
      <div id="wizard-body"></div>
    </div>`;
  renderWizardStep();
}
V.renderSetup = renderSetup;
window.renderSetup = renderSetup;

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

function cmdBlock(id, text) {
  return `<div class="cmd-block">
    <pre>${esc(text)}</pre>
    <button class="copy-btn" data-copy="${esc(id)}">Copy</button>
  </div>`;
}

function bindCopyButtons(texts) {
  document.querySelectorAll(".copy-btn").forEach((b) => {
    b.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(texts[b.dataset.copy]);
        b.textContent = "Copied ✓";
        setTimeout(() => { b.textContent = "Copy"; }, 1800);
      } catch {
        toast("Could not access the clipboard", true);
      }
    });
  });
}

async function renderWizardStep() {
  const w = App.wizard;
  const body = $("#wizard-body");
  const s = App.state;

  if (w.step === 0) {
    body.innerHTML = `<div class="card">
      <h2>Welcome</h2>
      <p>This wizard sets up <b>your side</b> of an AgentBridge — the link that
      lets your local Claude work hand-in-hand with a remote agent through a
      shared OneDrive/SharePoint folder.</p>
      <p>It will: choose where the app lives, check your system, connect the
      shared folder, create the bridge, install the Claude skills, and walk
      you through the remote machine's setup.</p>
      ${s.configured ? `<p class="hint">⚠ This machine is already set up
        (${esc(dn(s.role))} ⇄ ${esc(dn(s.peer))}). Re-running the wizard updates the
        configuration; automation settings are preserved.</p>` : ""}
      ${wizardNav(false, true, "Start")}</div>`;
    bindWizardNav();
  }

  else if (w.step === 1) {
    body.innerHTML = `<div class="card"><h2>Install location</h2>
      <p>Where should AgentBridge live on this PC?</p>
      <label class="choice-card ${w.mode === "install" ? "selected" : ""}" id="choice-install">
        <input type="radio" name="mode" ${w.mode === "install" ? "checked" : ""}>
        <span>
          <div class="choice-title">Install on this PC (recommended)</div>
          <div class="choice-desc">Copies the app to a proper location and adds
          Start Menu and Desktop shortcuts — like a normal installed program.</div>
          <div class="row" style="margin-top:9px">
            <input type="text" id="dest-input" style="flex:1;min-width:260px"
                   placeholder="Default: AppData\\Local\\AgentBridge" value="${esc(w.dest)}">
            <button id="dest-browse" type="button">Browse…</button>
          </div>
        </span>
      </label>
      <label class="choice-card ${w.mode === "portable" ? "selected" : ""}" id="choice-portable">
        <input type="radio" name="mode" ${w.mode === "portable" ? "checked" : ""}>
        <span>
          <div class="choice-title">Portable — run from where it is</div>
          <div class="choice-desc">Nothing is copied and no shortcuts are made.
          Good for USB sticks or trying it out.</div>
        </span>
      </label>
      <p id="install-result" class="hint">${w.installed
        ? `<span class="result-ok">✓ Installed to ${esc(w.installed.dest)}${w.installed.shortcuts
            ? " — Start Menu and Desktop shortcuts created" : " (shortcuts could not be created)"}</span>` : ""}</p>
      ${wizardNav(true, true, w.mode === "install" && !w.installed ? "Install" : "Next")}</div>`;
    const sync = (mode) => { w.mode = mode; renderSetup(); };
    $("#choice-install").addEventListener("click", () => { if (w.mode !== "install") sync("install"); });
    $("#choice-portable").addEventListener("click", () => { if (w.mode !== "portable") sync("portable"); });
    $("#dest-input").addEventListener("input", (e) => { w.dest = e.target.value; });
    $("#dest-browse").addEventListener("click", async (e) => {
      e.preventDefault();
      const r = await api("/api/pick_folder", {});
      if (r.path) { w.dest = r.path; $("#dest-input").value = r.path; }
    });
    bindWizardNav(async () => {
      if (w.mode !== "install" || w.installed) return true;
      const r = await api("/api/install_app", { dest: w.dest });
      if (r.error) {
        $("#install-result").innerHTML = `<span class="result-bad">✗ ${esc(r.error)}</span>`;
        return false;
      }
      w.installed = r;
      toast(r.already_there ? "Already running from the install location"
                            : "Installed — shortcuts created");
      return true;
    });
  }

  else if (w.step === 2) {
    body.innerHTML = `<div class="card"><h2>System checks</h2>
      <ul class="checklist" id="checks"><li>Running checks…</li></ul>
      ${wizardNav(true, true)}</div>`;
    bindWizardNav();
    const d = await api("/api/doctor");
    $("#checks").innerHTML = d.checks.map((c) => `
      <li><span class="${c.ok ? "ok" : "fail"}">${c.ok ? "✓" : "✗"}</span>
        ${esc(c.label)} <span class="detail">${esc(c.detail || "")}</span></li>`).join("");
  }

  else if (w.step === 3) {
    body.innerHTML = `<div class="card"><h2>Shared folder</h2>
      <p>Pick the locally-synced copy of the shared SharePoint folder.
      If you haven't synced it yet: open the folder in your browser and click
      <i>"Add shortcut to My files"</i>, then wait for OneDrive to sync it.</p>
      <div class="row">
        <input type="text" id="shared-input" style="flex:1;min-width:280px"
               placeholder="C:\\Users\\you\\OneDrive - …" value="${esc(w.shared)}">
        <button id="browse-btn">Browse…</button>
      </div>
      <p id="validate-result" class="hint">${w.validated?.ok
        ? `<span class="result-ok">✓ Folder is writable${w.validated.looks_synced ? " and looks synced" : ""}</span>` : ""}</p>
      ${wizardNav(true, true)}</div>`;
    $("#browse-btn").addEventListener("click", async () => {
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
      if (!v.looks_synced) toast("Note: the path doesn't look like a OneDrive/SharePoint folder", true);
      return true;
    });
  }

  else if (w.step === 4) {
    body.innerHTML = `<div class="card"><h2>Identity</h2>
      <p>Who is this machine, and who is on the other side? The defaults fit
      the standard analyst setup (your Claude ⇄ remote CoCo).</p>
      <dl class="kv" style="max-width:480px">
        <dt>My role</dt><dd><input type="text" id="role-input" value="${esc(w.role)}"></dd>
        <dt>Remote role</dt><dd><input type="text" id="peer-input" value="${esc(w.peer)}"></dd>
        <dt>How they work</dt><dd>
          <select id="relation-input" style="max-width:100%">
            <option value="drive" ${w.relation === "drive" ? "selected" : ""}>This side drives the remote agent</option>
            <option value="sym" ${w.relation === "sym" ? "selected" : ""}>Symmetrical — both sides autonomous</option>
            <option value="manual" ${w.relation === "manual" ? "selected" : ""}>Manual — people relay on both sides</option>
          </select></dd>
      </dl>
      <p class="hint">Custom names work too (e.g. analyst ⇄ sqlbot) — any pair,
      as long as they differ. "Drives" just means the other side answers
      automatically (it runs a handler); the protocol itself is symmetrical.</p>
      ${wizardNav(true, true)}</div>`;
    bindWizardNav(async () => {
      const role = $("#role-input").value.trim();
      const peer = $("#peer-input").value.trim();
      if (!role || !peer || role === peer) {
        toast("The two roles must be filled in and different", true);
        return false;
      }
      w.role = role; w.peer = peer;
      w.relation = $("#relation-input").value;
      return true;
    });
  }

  else if (w.step === 5) {
    body.innerHTML = `<div class="card"><h2>Create bridge</h2>
      <dl class="kv">
        <dt>Sides</dt><dd>${esc(dn(w.role))} ⇄ ${esc(dn(w.peer))}</dd>
        <dt>Shared folder</dt><dd class="mono">${esc(w.shared)}</dd>
        <dt>Settings stored in</dt><dd class="mono">${esc(s.home)}</dd>
      </dl>
      <p id="init-result" class="hint">${w.initDone ? '<span class="result-ok">✓ Bridge created</span>' : ""}</p>
      ${wizardNav(true, true, w.initDone ? "Next" : "Create")}</div>`;
    bindWizardNav(async () => {
      if (w.initDone) return true;
      const r = await api("/api/init", { role: w.role, peer: w.peer, shared: w.shared });
      if (r.error) {
        $("#init-result").innerHTML = `<span class="result-bad">✗ ${esc(r.error)}</span>`;
        return false;
      }
      w.initDone = true;
      await V.refresh(true);
      return true;
    });
  }

  else if (w.step === 6) {
    body.innerHTML = `<div class="card"><h2>Claude skills</h2>
      <p>Installs the AgentBridge skills so your Claude Code sessions know how
      to drive the bridge — checking messages, sending tasks, receiving results.</p>
      <p id="skills-result" class="hint">${w.skills
        ? `<span class="result-ok">✓ Installed: ${esc(w.skills.join(", "))}</span>` : ""}</p>
      <p class="hint">This covers Claude Code (CLI, desktop and IDE). For
      claude.ai chat, upload the pre-built zips from the app's
      <code>skills\\</code> folder via Settings → Capabilities → Skills.</p>
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

  else if (w.step === 7) {
    body.innerHTML = `<div class="card"><h2>Remote agent</h2>
      <p>Loading the personalized guide…</p></div>`;
    const g = await api("/api/remote_guide");
    if (g.error) {
      body.innerHTML = `<div class="card"><h2>Remote agent</h2>
        <p class="result-bad">✗ ${esc(g.error)} — create the bridge first (step 6).</p>
        ${wizardNav(true, true, "Skip for now")}</div>`;
      bindWizardNav();
      return;
    }
    const remoteShared = `C:\\Users\\<username>\\${g.sync_segment}\\${g.shared_leaf}`;
    const binFile = g.published_file || "bridge_<newest>.py";
    const texts = {
      install: [
        `mkdir C:\\AgentBridge`,
        `copy "${remoteShared}\\bin\\${binFile}" C:\\AgentBridge\\bridge.py`,
        `cd C:\\AgentBridge`,
        `python bridge.py init --role ${g.peer} --peer ${g.role} --shared "${remoteShared}"`,
        `python bridge.py doctor`,
        `python bridge.py send "${dn(g.peer)} online" --type ping`,
      ].join("\n"),
      autostart: `schtasks /create /tn "AgentBridge Watch" /sc onlogon /tr "cmd /c cd /d C:\\AgentBridge && python bridge.py watch >> watch.out.log 2>&1"`,
      handler: `python C:\\AgentBridge\\bridge.py init --role ${g.peer} --peer ${g.role} --shared "${remoteShared}" --handler-cmd "python C:\\AgentBridge\\handler_coco.py {body_file} {seq}" --handler-timeout 3600`,
    };
    body.innerHTML = `<div class="card"><h2>Remote agent (${esc(dn(g.peer))}'s machine)</h2>
      <p>The other half of the bridge runs on the remote machine. These steps
      happen <b>on that machine</b> — about 10 minutes. Replace
      <code>&lt;username&gt;</code> with that machine's Windows account name.</p>

      <div class="guide-step"><h3><span class="n">1</span> Sync the shared folder</h3>
        <p class="hint">Sign OneDrive into the work account, open the shared folder
        in the browser and click <i>"Add shortcut to My files"</i>. It will appear at
        <code>${esc(remoteShared)}</code>.</p>
      </div>

      <div class="guide-step"><h3><span class="n">2</span> Install the bridge</h3>
        <p class="hint">The app is already in the shared folder — no download needed.
        Run in PowerShell or Command Prompt:</p>
        ${cmdBlock("install", texts.install)}
      </div>

      <div class="guide-step"><h3><span class="n">3</span> Keep it listening</h3>
        <p class="hint">Auto-start the listener at every logon (or just keep a
        terminal open running <code>python bridge.py watch</code>):</p>
        ${cmdBlock("autostart", texts.autostart)}
      </div>

      <div class="guide-step"><h3><span class="n">4</span> Brief the remote agent</h3>
        <p class="hint">Paste the operating prompt (in the full guide, step 4)
        into an interactive session of the remote agent so it knows the rules
        of the bridge.</p>
        <button onclick="openTarget('remote_md')">Open the full guide</button>
      </div>

      <div class="guide-step"><h3><span class="n">5</span> Full automation
        ${w.relation === "manual" ? "(skip — you chose manual relay)" : "(optional)"}</h3>
        <p class="hint">A handler can run the remote agent automatically for every
        message.${w.relation === "sym" ? " You chose a symmetrical bridge — repeat this on <b>both</b> machines so each side answers on its own." : ""}
        Send the kit through the bridge, then run the command below on
        the remote machine. <b>Careful:</b> this init must always include the
        handler flags — running a plain init afterwards silently removes them.</p>
        <div class="row" style="margin-bottom:6px">
          <button id="send-kit-btn" ${!g.handler_available ? "disabled" : ""}>
            ${w.kitSent ? "Kit sent ✓" : `Send automation kit to ${esc(dn(g.peer))}`}</button>
        </div>
        ${cmdBlock("handler", texts.handler)}
      </div>
      ${wizardNav(true, true)}</div>`;
    bindCopyButtons(texts);
    bindWizardNav();
    $("#send-kit-btn").addEventListener("click", async (e) => {
      if (w.kitSent) return;
      if (!confirm(`Send handler_coco.py, disallowed_tools.json and REMOTE_SETUP.md to ${dn(g.peer)} over the bridge?`)) return;
      e.target.disabled = true;
      const r = await api("/api/send_remote_kit", {});
      if (r.error) { toast(r.error, true); e.target.disabled = false; return; }
      w.kitSent = true;
      e.target.textContent = "Kit sent ✓";
      toast("Automation kit sent over the bridge");
    });
  }

  else {
    body.innerHTML = `<div class="card"><h2>All set 🎉</h2>
      <p>This machine is ready${w.role ? ` as <b>${esc(dn(w.role))}</b>` : ""}.</p>
      <ul>
        ${w.installed ? `<li>App installed to <code>${esc(w.installed.dest)}</code> — use the Start Menu shortcut next time.</li>` : ""}
        <li>Send a first <b>ping</b> from the Chat page to test the line.</li>
        <li>Once the remote side is up, everything else happens by itself.</li>
      </ul>
      <div class="wizard-nav">
        <button class="primary" onclick="location.hash='#/messages'">Open chat</button>
        <button id="wiz-restart">Run the wizard again</button>
      </div></div>`;
    $("#wiz-restart").addEventListener("click", () => {
      App.wizard = freshWizard();
      renderSetup();
    });
  }
}
