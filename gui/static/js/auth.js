/* The signed-out surface (R53/V34): sign-in / create-account as a dedicated
   FULL PAGE instead of a card inside the app shell — the first brick of the
   setup pages (packaging extends this surface for first-run setup). It
   inherits the R48 boot identity (glyph, title, E2EE note), so the boot
   cover fades onto it seamlessly.

   Live username checking (V24): format problems surface instantly from a
   client mirror of the server rules; existence rides a debounced
   /api/mesh/check_name (the pre-auth directory reader). The error line
   under the username is a 0fr grid row that expands, easing the password
   field down. Layer: a page view — registers on V, imports only downward. */

import { $, esc, toast } from "./util.js";
import { api } from "./api.js";
import { Mesh } from "./state.js";
import { openModal, closeModal } from "./modal.js";
import { V } from "./views.js";

// client mirror of accounts.py valid_name — instant feedback, zero
// round-trips; the server stays authoritative (signup re-checks)
const NAME_RE = /^[a-z][a-z0-9_-]{1,31}$/;
const RESERVED = new Set(["all", "everyone", "here", "admin", "system", "info", "mesh"]);
const FORMAT_HINT = "2–32 characters: lowercase letters, digits, _ or ‑, "
  + "starting with a letter";

let checkSeq = 0;   // ignore out-of-order check responses while typing

function renderAuthPage(force = false) {
  // already showing: an external caller (route change, signed-out poll) must
  // never clobber half-typed credentials — only the page's own tab toggle
  // re-renders (force). R56/V40.
  if ($("#auth") && !force) return;
  const mode = Mesh.auth.mode;
  // half-typed values survive the Sign in / Create account toggle
  const held = {
    user: $("#auth-user")?.value ?? "",
    pass: $("#auth-pass")?.value ?? "",
    display: $("#auth-display")?.value ?? "",
  };
  let root = $("#auth");
  if (!root) {
    root = document.createElement("div");
    root.id = "auth";
    document.body.appendChild(root);
  }
  root.innerHTML = `
    <div class="auth-inner">
      <div class="boot-glyph">
        <svg viewBox="0 0 32 32" width="34" height="34"><path d="M4 22c3.5-8 20.5-8 24 0M4 22v-4M28 22v-4" stroke="white" stroke-width="3" fill="none" stroke-linecap="round"/></svg>
      </div>
      <div class="boot-title">AgentBridge</div>
      <div class="auth-card">
        <div class="auth-tabs">
          <button id="auth-login" class="${mode === "login" ? "sel" : ""}">Sign in</button>
          <button id="auth-signup" class="${mode === "signup" ? "sel" : ""}">Create account</button>
        </div>
        <label class="auth-fld"><span>Username</span>
          <input type="text" id="auth-user" autocomplete="username" spellcheck="false"></label>
        <div class="auth-err" id="auth-user-err" role="alert"><div><span></span></div></div>
        ${mode === "signup" ? `<label class="auth-fld"><span>Display name</span>
          <input type="text" id="auth-display" autocomplete="name"></label>` : ""}
        <label class="auth-fld"><span>Password</span>
          <input type="password" id="auth-pass"
            autocomplete="${mode === "signup" ? "new-password" : "current-password"}"></label>
        <div class="auth-err" id="auth-sub-err" role="alert"><div><span></span></div></div>
        <button class="primary auth-go" id="auth-go">${mode === "signup" ? "Create account" : "Sign in"}</button>
        <p class="hint">Accounts live in the shared mesh — one account works
        from any machine that syncs it.</p>
      </div>
      <div class="boot-note">
        <svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></svg>
        End-to-end encrypted
      </div>
    </div>`;
  const userIn = $("#auth-user");
  const passIn = $("#auth-pass");
  if (held.user) userIn.value = held.user;
  if (held.pass) passIn.value = held.pass;
  const disp = $("#auth-display");
  if (disp && held.display) disp.value = held.display;

  $("#auth-login").addEventListener("click", () => {
    Mesh.auth.mode = "login"; renderAuthPage(true);
  });
  $("#auth-signup").addEventListener("click", () => {
    Mesh.auth.mode = "signup"; renderAuthPage(true);
  });

  // V39: submit refusals surface IN the card (a toast renders under this
  // full-page overlay — z-index made honest too, but the card is the fix)
  const subBox = $("#auth-sub-err");
  const setSubError = (msg) => {
    subBox.querySelector("span").textContent = msg;
    subBox.classList.toggle("show", !!msg);
  };
  passIn.addEventListener("input", () => setSubError(""));
  disp?.addEventListener("input", () => setSubError(""));

  // ---- live username checking (V24) ----
  const errBox = $("#auth-user-err");
  const setNameError = (msg) => {
    errBox.querySelector("span").textContent = msg;
    errBox.classList.toggle("show", !!msg);
  };
  let debounce = null;
  userIn.addEventListener("input", () => {
    clearTimeout(debounce);
    setSubError("");                  // editing invalidates the old refusal
    checkSeq++;                       // anything in flight is stale now
    const name = userIn.value.trim().toLowerCase();
    if (!name) { setNameError(""); return; }
    if (!NAME_RE.test(name)) {
      // an invalid name can't exist either way — phrase per mode
      setNameError(mode === "signup" ? FORMAT_HINT
        : `No account is named @${name}`);
      return;
    }
    if (RESERVED.has(name)) {
      setNameError(mode === "signup" ? `@${name} is reserved`
        : `No account is named @${name}`);
      return;
    }
    setNameError("");                 // format fine — existence is async
    debounce = setTimeout(async () => {
      const seq = ++checkSeq;
      const r = await api("/api/mesh/check_name", { username: name })
        .catch(() => null);
      // stale (kept typing), server without the endpoint, or a transport
      // hiccup: stay quiet — submit still validates authoritatively
      if (seq !== checkSeq || !r || r.error || !r.ok) return;
      if (userIn.value.trim().toLowerCase() !== r.name) return;
      if (mode === "signup") {
        setNameError(r.taken ? `@${r.name} is already taken` : "");
      } else {
        setNameError(r.taken ? "" : `No account named @${r.name} on this mesh yet`);
      }
    }, 300);
  });

  const go = async () => {
    const btn = $("#auth-go");
    if (btn.disabled) return;
    btn.disabled = true;
    try {
      const payload = {
        username: userIn.value.trim(),
        password: passIn.value,
        display: $("#auth-display")?.value?.trim(),
      };
      const r = await api(mode === "signup" ? "/api/mesh/signup" : "/api/mesh/login", payload);
      if (r.error) { setSubError(r.error); return; }  // V39: in-card, never a hidden toast
      // D5: the recovery code is shown ONCE — at signup, and on the first v2
      // sign-in of a migrated account (identity keys freshly minted)
      if (r.recovery_code) await showRecoveryCode(r.recovery_code);
      closeAuthPage();
      V.renderChats(true);
    } finally {
      btn.disabled = false;
    }
  };
  $("#auth-go").addEventListener("click", go);
  passIn.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  userIn.addEventListener("keydown", (e) => { if (e.key === "Enter") passIn.focus(); });
  if (!held.user) userIn.focus();
}

function closeAuthPage() {
  $("#auth")?.remove();
}

// D5 recovery code — shown ONCE. Encryption keys are wrapped by the password
// AND by this code; if the password is ever forgotten, this code is the only
// way back into the account's history. The Continue button is gated on an
// explicit "I've saved it" so it can't be dismissed by reflex.
function showRecoveryCode(code) {
  return new Promise((resolve) => {
    const box = openModal(`
      <div class="cf-title">Save your recovery code</div>
      <div class="cf-body" style="text-align:left">
        This is the <b>only</b> way back into your account if you forget your
        password. It is shown once and never again — store it somewhere safe.
      </div>
      <div class="recovery-code" id="rc-code">${esc(code)}</div>
      <div class="row" style="justify-content:center;margin:10px 0">
        <button id="rc-copy">Copy code</button>
      </div>
      <label class="rc-ack">
        <input type="checkbox" id="rc-ack"> I have saved my recovery code
      </label>
      <div class="cf-actions">
        <button class="cf-pill" id="rc-go" disabled>Continue</button>
      </div>`);
    box.classList.add("confirm");
    box.parentElement.classList.add("confirm-scrim");
    box.querySelector("#rc-copy").addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(code);
        toast("Recovery code copied", { check: true });
      } catch { toast("Could not access the clipboard", true); }
    });
    const ack = box.querySelector("#rc-ack");
    const goBtn = box.querySelector("#rc-go");
    ack.addEventListener("change", () => { goBtn.disabled = !ack.checked; });
    goBtn.addEventListener("click", () => { closeModal(); resolve(); });
  });
}

V.renderAuthPage = renderAuthPage;
V.closeAuthPage = closeAuthPage;
