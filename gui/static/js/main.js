/* Entry point: hash router, poll loop, shell chrome. Importing the view
   modules below registers them on V (see views.js for the layering rules). */

import { $, initTheme, initAccent, toast } from "./util.js";
import { api } from "./api.js";
import { App, Mesh, Settings, resetSubviews, renderChrome } from "./state.js";
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
import "./wizard.js";

async function refresh(rerender) {
  try {
    App.state = await api("/api/state");
  } catch {
    return;  // server unreachable; next poll retries
  }
  // open/close the SSE stream to match the current server + auth (inert on v1)
  syncRealtime();
  renderChrome();
  if (rerender && App.page !== "setup") PAGES[App.page]();
  else if (App.page === "chats" && Mesh.state?.user) V.renderChats(false);
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
  setup: () => V.renderSetup(),   // hidden from the UI; reachable while unconfigured
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
  PAGES[App.page]();
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
  App.state = await api("/api/state");
  if (!location.hash) {
    location.hash = App.state.configured ? "#/chats" : "#/setup";
  }
  route();
  // R48: drop the full-page boot cover once the FIRST real view painted —
  // Mesh.state present (sidebar + chat/auth rendered) or a non-chats page
  // routed. Safety cap: an error view is better shown than hidden.
  (function bootDone() {
    const b = document.getElementById("boot");
    if (!b) return;
    const t0 = Date.now();
    (function tick() {
      if (!Mesh.state && App.page === "chats" && Date.now() - t0 < 15000) {
        setTimeout(tick, 80); return;
      }
      b.classList.add("done");
      setTimeout(() => b.remove(), 350);
    })();
  })();
  // poll cadence is user-tunable (Settings → Connection); re-read each tick so
  // a change applies without a reload. When the SSE stream is live (v2) the
  // stream carries the news, so the poll drops to a slow safety-net tick that
  // heals any dropped frame without hammering the server.
  (function poll() {
    const base = Math.max(1000, +(localStorage.getItem("pollMs") || 2500) || 2500);
    const ms = realtimeActive() ? Math.max(base, 20000) : base;
    setTimeout(async () => {
      try { await refresh(false); } catch { /* next tick retries */ }
      poll();
    }, ms);
  })();
})();
