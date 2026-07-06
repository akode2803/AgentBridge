/* Entry point: hash router, poll loop, shell chrome. Importing the view
   modules below registers them on V (see views.js for the layering rules). */

import { $, initTheme, toast } from "./util.js";
import { api } from "./api.js";
import { App, Mesh, Settings, resetSubviews, renderChrome } from "./state.js";
import { V, EXPECTED } from "./views.js";
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
  renderChrome();
  if (rerender && App.page !== "setup") PAGES[App.page]();
  else if (App.page === "chats" && Mesh.state?.user) V.renderChats(false);
}
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
  $("#side-new").addEventListener("click", () => { location.hash = "#/new"; });
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
  if (savedW) $("#navrail").style.width = savedW + "px";
  $("#side-resizer").addEventListener("dblclick", () => {
    document.body.classList.add("side-collapsed");
    localStorage.setItem("sideCollapsed", "1");
  });
  $("#side-resizer").addEventListener("mousedown", (e) => {
    e.preventDefault();
    const move = (ev) => {
      const w = Math.min(480, Math.max(220, ev.clientX - 58));
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
  // poll cadence is user-tunable (Settings → Connection); re-read each
  // tick so a change applies without a reload
  (function poll() {
    const ms = Math.max(1000, +(localStorage.getItem("pollMs") || 2500) || 2500);
    setTimeout(async () => {
      try { await refresh(false); } catch { /* next tick retries */ }
      poll();
    }, ms);
  })();
})();
