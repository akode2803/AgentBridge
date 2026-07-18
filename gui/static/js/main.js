/* Entry point: hash router, poll loop, shell chrome. Importing the view
   modules below registers them on V (see views.js for the layering rules). */

import { $, initTheme, initAccent, toast } from "./util.js";
import { api } from "./api.js";
import { App, Mesh, Settings, RESTART_KEY, restartIntent, clearRestartIntent,
         resetSubviews, renderChrome } from "./state.js";
import { renderSidebar } from "./sidebar.js";
import { V, EXPECTED } from "./views.js";
import { syncRealtime, realtimeActive } from "./realtime.js";
import "./auth.js";
import "./chat.js";
import "./details.js";
import "./media.js";
import "./search.js";
import "./members.js";
import "./forward.js";
import "./settings.js";

// V127: the idle auto-lock clock. Real input bumps it (listeners in init
// below), and so do the auth transitions detected in refresh() — an
// AUTOMATIC re-sign-in (V122 restore) or an unlock arrives with zero
// keystrokes, and a 5-min window that expired while the app was busy
// recovering must not lock it the moment it becomes usable.
let lastActive = Date.now();
const bumpIdle = () => { lastActive = Date.now(); };
let prevAuth = { user: null, locked: false };
// a manual lock raises the cover BEFORE its POST lands — the poll's heal
// below must not eat that optimistic cover (set/cleared by lockNow)
let lockPending = false;
// V125: consecutive failed /api/state fetches = a restart's down window
let downTicks = 0;
// V131: the server version this PAGE booted against. An update+restart
// changes it under us, and the old frontend would keep running until a
// manual reload — arm a one-shot reload instead, taken at a safe moment.
let bootVersion = "";
let reloadArmed = false;
let restartWatchActive = false;

function watchRestartGeneration() {
  if (restartWatchActive) return;
  restartWatchActive = true;
  (async function tick() {
    if (!restartIntent()) {
      restartWatchActive = false;
      return;
    }
    try { await refresh(false); } catch { /* the next probe heals */ }
    if (!restartIntent()) {
      restartWatchActive = false;
      return;
    }
    setTimeout(tick, 750);
  })();
}

function showRestarting() {
  V.renderConnectingPage("Restarting…");
  // The normal realtime safety poll may be 20s away. During a requested
  // restart, probe only localhost at a short cadence until the process
  // generation changes so the cover reads as progress instead of a hang.
  watchRestartGeneration();
}

document.addEventListener("ab:restart", showRestarting);
window.addEventListener("storage", (e) => {
  if (e.key !== RESTART_KEY) return;
  if (restartIntent()) showRestarting();
  else {
    Mesh.state = null;
    Promise.resolve(refresh(true)).catch(() => {});
  }
});

async function refresh(rerender) {
  try {
    App.state = await api("/api/state");
  } catch {
    // V125: the server staying unreachable is a restart's down window — a
    // frozen page with no signal read as "restart app does not work" (live
    // report: a second restart clicked 19s after the first came back).
    // Two misses raise the boot surface; the lock page, when up, already
    // covers everything. The first miss re-probes once in 3s — the poll
    // CHAIN may still be a slow SSE-era tick away, and a ~20s restart
    // window fit entirely between two ticks (verified live on a rig).
    downTicks += 1;
    if (downTicks >= 2 && !document.getElementById("lock")) {
      V.renderConnectingPage("Restarting…");
    } else if (downTicks === 1) {
      setTimeout(() => { Promise.resolve(refresh(false)).catch(() => {}); },
                 3000);
    }
    return;  // server unreachable; next poll retries
  }
  downTicks = 0;
  // V131: the server was updated under this page — reload ONCE to pick up
  // the matching frontend, but never mid-thought: not over an open modal,
  // not over a half-typed message. Until it's safe, keep polling armed.
  const v = App.state.gui_version || "";
  if (!bootVersion) bootVersion = v;
  else if (v && v !== bootVersion) reloadArmed = true;
  if (reloadArmed
      && !document.querySelector(".modal-scrim")
      && !document.getElementById("mesh-body")?.value) {
    location.reload();
    return;
  }
  // V127: signed-out -> signed-in and locked -> unlocked count as activity.
  // Discrete state deltas only — the poll itself must never bump, or the
  // idle timer would stop working entirely.
  const was = prevAuth;
  prevAuth = { user: App.state.user || null,
               locked: !!App.state.app_lock?.locked };
  if ((!was.user && prevAuth.user) || (was.locked && !prevAuth.locked)) {
    bumpIdle();
  }
  const restarting = restartIntent();
  // The POSTing client may still reach the draining old server for a few
  // seconds. Its process generation must change before any window drops the
  // shared cover or considers a userless response authoritative.
  if (restarting && restarting.instance
      && App.state.instance_id === restarting.instance) {
    showRestarting();
    return;
  }
  // V111: locked = the lock page and nothing else (no SSE churn, no page
  // renders over it) — the poll keeps watching /api/state, which answers
  // while locked, so unlocking elsewhere heals this window too
  if (App.state.app_lock?.locked) {
    if (restarting) clearRestartIntent();
    V.renderLockPage();
    return;
  }
  // V127: the heal promised above, made real — a lock page still up while
  // the server says unlocked (another window's unlock, the account password
  // over the API) fades away onto the app rendered below
  if (!lockPending && document.getElementById("lock")) V.closeLockPage();
  if (restarting) {
    if (!App.state.user) {
      V.renderConnectingPage(App.state.restoring
        ? "Connecting to your mesh…" : "Restarting…");
      return;
    }
    clearRestartIntent();
    Mesh.state = null;
    V.closeConnectingPage();
    rerender = true;
  }
  const recoveringCover = !!document.getElementById("connecting");
  if (!restarting && App.state.user && recoveringCover) {
    Mesh.state = null;
    rerender = true;
  }
  // open/close the SSE stream to match the current server + auth (inert on v1)
  syncRealtime();
  renderChrome();
  if (rerender) {
    try { await PAGES[App.page](); } catch { /* the next poll heals */ }
    if (App.state.user) V.closeConnectingPage();
  }
  else if (App.page === "chats" && Mesh.state?.user) V.renderChats(false);
  // V122: the server came back (App.state just fetched fine) but this page
  // never got a mesh state — a reload that landed during a restart's down
  // window used to sit on the dropped boot cover forever, reading as a
  // sign-out. Kick a full chats render; its own fetch fills Mesh.state.
  else if (!Mesh.state && PAGES[App.page]) {
    Promise.resolve(PAGES[App.page]()).catch(() => {});
  }
  // signed out (R53): watch for a session appearing OUTSIDE the auth page —
  // another window, setup assist, the CLI. Never re-render the auth page
  // from the poll (it would clobber half-typed fields); flip only when a
  // user actually shows up.
  else if (App.page === "chats" && Mesh.state && !Mesh.state.user) {
    const fresh = await api("/api/mesh/state");
    if (!fresh.error && fresh.user) {
      Mesh.state = fresh;
      V.renderChats(true);
    }
  }
  // R51 (V25): the "new" page's directory pickers were frozen while open —
  // refresh + repaint them per tick too (setSide no-ops on identical html).
  // The picker's search box can sit FOCUSED while empty, so the guard keys
  // on an actual query in progress, not focus; when a changed list does
  // repaint, the (empty) box gets its focus back.
  else if (App.page === "new" && Mesh.state?.user) {
    const ae = document.activeElement;
    const inSide = ae && $("#side-chats")?.contains(ae);
    if (inSide && ae.tagName === "INPUT" && ae.value) return;
    const hadFocus = inSide && ae.id ? ae.id : null;
    const fresh = await api("/api/mesh/state");
    if (!fresh.error && App.page === "new") {
      Mesh.state = fresh;
      renderSidebar();
      if (hadFocus) document.getElementById(hadFocus)?.focus();
    }
  }
}
// the settings page runs its own leash (settings.js startSettingsPoll) —
// its data lives outside /api/state, and repaints need interaction guards
V.refresh = refresh;

// a missing registration is a wiring bug — fail loudly at boot, not with
// an undefined-call deep inside a render
for (const name of EXPECTED) {
  if (typeof V[name] !== "function") {
    toast(`Front-end wiring error: V.${name} is not registered`, true);
    throw new Error(`V.${name} missing — check the imports in main.js`);
  }
}

const PAGES = {
  chats: () => V.renderChats(true),
  new: () => V.renderNewChat(),
  settings: () => V.renderSettings(),
  // the bridge-era setup wizard is RETIRED (R56/V40) — the real setup pages
  // are the packaging session's; the auth page (auth.js) is their first brick
};

function route() {
  const hash = location.hash.replace("#/", "");
  const [page0, sub, sub2] = hash.split("/");
  let page = page0 || "chats";
  if (page === "agents") page = "settings";        // merged into settings
  if (!PAGES[page]) page = "chats";                // home/messages retired
  App.page = page;
  if (page === "chats") {
    const chatId = sub || null;
    const details = sub2 === "details";
    if (chatId !== Mesh.chatId) {
      Mesh.chatKey = "";
      Mesh.listKey = "";
      Mesh.structKey = "";
      Mesh.msgExpand = {};   // expanded "Read more" messages collapse on switch
      resetSubviews();
      // blank surface while the chat loads — the mobile slide-in shows
      // this instead of the previous page flashing by
      if (chatId) $("#content").innerHTML = '<div class="chat-loading"></div>';
    }
    if (details !== Mesh.detailsView) {
      Mesh.detailsKey = "";
      if (!details) resetSubviews();
    }
    Mesh.chatId = chatId;
    Mesh.detailsView = details;
    // hide the info pane synchronously when leaving it, so the chat reclaims
    // the freed width in the same frame as the route change. renderChats
    // fetches mesh state before it could hide the pane, which left the emptied
    // pane holding its column for ~300ms, then snapped the chat wider (stutter
    // on medium screens where the pane covers the chat).
    if (!details) {
      const dp = $("#details-pane");
      if (dp && !dp.hidden) { dp.hidden = true; dp.innerHTML = ""; Mesh.detailsKey = ""; }
    }
  } else {
    Mesh.chatId = null;
    Mesh.detailsView = false;
    resetSubviews();
    Mesh.structKey = "";
  }
  if (page !== "new") Mesh.newMode = "dm";
  $("#side-new").hidden = page !== "chats";
  Settings.section = page === "settings" ? (sub || null) : null;
  $("#content").classList.toggle("chat-mode",
    App.page === "chats" && !!Mesh.chatId);
  // mobile: pane-open = an open chat or a chosen settings section fills
  // the screen; the sidebar is the home surface otherwise
  document.body.classList.toggle("pane-open",
    (page === "chats" && !!Mesh.chatId) || (page === "settings" && !!sub));
  document.body.classList.toggle("details-open",
    page === "chats" && !!Mesh.detailsView);
  $("#rail-chats").classList.toggle("active", page === "chats" || page === "new");
  $("#rail-account").classList.toggle("active", page === "settings");
  renderChrome();
  Promise.resolve(PAGES[App.page]()).catch(() => {});
}

window.addEventListener("hashchange", route);

(async function start() {
  initTheme();
  initAccent();
  $("#side-new").addEventListener("click", () => { location.hash = "#/new"; });
  // the brand header is home: the default (no active chat) window, which now
  // carries the app-level Connection details + the stand-down switch (round E7).
  $("#side-head").addEventListener("click", () => { location.hash = "#/chats"; });
  // rail: navigate; clicking ANY rail item also brings back a collapsed
  // sidebar (was active-item-only — user found that surprising)
  const railGo = (target) => {
    if (document.body.classList.contains("side-collapsed")) {
      document.body.classList.remove("side-collapsed");
      localStorage.removeItem("sideCollapsed");
    }
    location.hash = target;
  };
  $("#rail-chats").addEventListener("click", () => railGo("#/chats"));
  $("#rail-account").addEventListener("click", () => railGo("#/settings"));
  if (localStorage.getItem("sideCollapsed")) {
    document.body.classList.add("side-collapsed");
  }
  // resizable sidebar, width persisted; double-click collapses it
  const savedW = parseInt(localStorage.getItem("sidebarW"), 10);
  // clamp a saved width to the current bounds (older builds allowed narrower)
  if (savedW) $("#navrail").style.width = Math.min(560, Math.max(260, savedW)) + "px";
  $("#side-resizer").addEventListener("dblclick", () => {
    document.body.classList.add("side-collapsed");
    localStorage.setItem("sideCollapsed", "1");
  });
  $("#side-resizer").addEventListener("mousedown", (e) => {
    e.preventDefault();
    const move = (ev) => {
      const w = Math.min(560, Math.max(260, ev.clientX - 58));
      $("#navrail").style.width = w + "px";
      localStorage.setItem("sidebarW", w);
    };
    const up = () => {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  });
  // V43 (R57): type-to-compose — with a chat open, a printable key pressed
  // anywhere lands in the composer (WhatsApp). Focusing during keydown lets
  // the browser deliver the character to the textarea natively; real inputs,
  // modals, menus and shortcuts are never hijacked.
  document.addEventListener("keydown", (e) => {
    if (App.page !== "chats" || !Mesh.chatId) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key.length !== 1) return;             // printable characters only
    const box = document.getElementById("mesh-body");
    if (!box || document.activeElement === box) return;
    const ae = document.activeElement;
    if (ae && (ae.tagName === "INPUT" || ae.tagName === "TEXTAREA"
        || ae.isContentEditable)) return;
    if (document.querySelector(".modal-scrim, .csel-pop, .menu")) return;
    box.focus();
  });
  // while the user is selecting text inside a message, suppress the hover
  // reply-arrow — otherwise the chevron pops up over the selection
  document.addEventListener("selectionchange", () => {
    const content = $("#content");
    if (!content) return;
    const sel = document.getSelection();
    const active = !!(sel && !sel.isCollapsed && sel.rangeCount
      && $("#transcript")?.contains(sel.anchorNode));
    content.classList.toggle("sel-text", active);
  });
  if (restartIntent()) showRestarting();
  try {
    App.state = await api("/api/state");
  } catch {
    downTicks = 2;
    V.renderConnectingPage(restartIntent() ? "Restarting…" : "Connecting…");
  }
  if (!location.hash) location.hash = "#/chats";
  route();
  // V45: the daily auto update check (About page pref, default on). Signed
  // out or offline it fails silently; a hit points at Settings → About.
  if (localStorage.getItem("updAuto") !== "0"
      && Date.now() - (+localStorage.getItem("updLastCheck") || 0) > 86400e3) {
    localStorage.setItem("updLastCheck", String(Date.now()));
    api("/api/update_check").then((r) => {
      if (r && r.ok && r.newer) {
        toast(`AgentBridge ${r.latest} is available — Settings → About`);
      }
    }).catch(() => {});
  }
  // V63: the daily storage sweep — silent maintenance (the About page's
  // "Clean up now" is the loud, on-demand version)
  if (Date.now() - (+localStorage.getItem("janLastRun") || 0) > 86400e3) {
    localStorage.setItem("janLastRun", String(Date.now()));
    api("/api/mesh/janitor", {}).catch(() => {});
  }
  // R48: drop the full-page boot cover once the FIRST real view painted —
  // Mesh.state present (sidebar + chat/auth rendered) or a non-chats page
  // routed. Safety cap: an error view is better shown than hidden.
  (function bootDone() {
    const b = document.getElementById("boot");
    if (!b) return;
    const t0 = Date.now();
    (function tick() {
      // V122: 45s cap — a restart's down window runs ~20s, and dropping the
      // cover onto a bare shell mid-boot read as "the app signed out".
      // V111: the lock page IS a real first view — fade onto it.
      // V125: so is the connecting page (blind restore in progress).
      if (!Mesh.state && !document.getElementById("lock")
          && !document.getElementById("connecting")
          && App.page === "chats" && Date.now() - t0 < 45000) {
        setTimeout(tick, 80); return;
      }
      b.classList.add("done");
      setTimeout(() => b.remove(), 350);
    })();
  })();
  // ---- V111 app lock: the client-side triggers ------------------------
  // any endpoint refusing with `locked` raises the screen (api.js event)
  document.addEventListener("ab:locked", () => V.renderLockPage());
  // manual lock — the settings card's "Lock now" button and Ctrl+L
  const lockNow = async () => {
    lockPending = true;                    // V127: shield from the poll heal
    V.renderLockPage();                    // cover FIRST, then tell the server
    try { await api("/api/applock/lock", {}); } catch { /* poll heals */ }
    if (App.state?.app_lock) App.state.app_lock.locked = true;
    lockPending = false;
  };
  window.lockAppNow = lockNow;
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "l"
        && App.state?.app_lock?.enabled && !document.getElementById("lock")) {
      e.preventDefault();
      lockNow();
    }
  });
  // idle auto-lock: user input bumps the clock (module-scope, V127 — auth
  // transitions in refresh() bump it too); a slow sweep compares it to the
  // owner-set window (0 = manual only)
  for (const ev of ["pointerdown", "keydown", "wheel", "mousemove"]) {
    document.addEventListener(ev, bumpIdle, { capture: true, passive: true });
  }
  setInterval(() => {
    const lk = App.state?.app_lock;
    if (!lk?.enabled || lk.locked || document.getElementById("lock")) return;
    if (lk.autolock_min > 0
        && Date.now() - lastActive > lk.autolock_min * 60000) {
      lockNow();
    }
  }, 5000);
  // Local /api/state poll — fixed cadence (the user knob retired in V110:
  // this only hits our own localhost server's in-memory mirror; every cadence
  // that costs anything is profile-driven in the transport layer since R76).
  // When the SSE stream is live (v2) the stream carries the news, so the poll
  // drops to a slow safety-net tick that heals any dropped frame.
  (function poll() {
    const ms = realtimeActive() ? 20000 : 2500;
    setTimeout(async () => {
      try { await refresh(false); } catch { /* next tick retries */ }
      poll();
    }, ms);
  })();
})();
