/* Canonical search advances through bounded history windows on demand. */
import { $, esc, timeOnly, dayLabel } from "./util.js";
import { ICONS } from "./icons.js";
import { Mesh, meshDn } from "./state.js";
import { readCollection, collectionControls } from "./detail_pages.js";
import { invalidateDetailsRead } from "./details_read.js";
import { V } from "./views.js";

let queryTimer = null;
async function renderChatSearch(mode = "refresh") {
  const chatId = Mesh.chatId;
  const pane = $("#details-pane");
  const identity = JSON.stringify([chatId, "search"]);
  if (pane.dataset.collection !== identity || !pane.querySelector("#cs-input")) {
    pane.dataset.collection = identity;
    pane.innerHTML = `<div class="pane-head">
      <button class="icon-btn" id="cs-back" aria-label="Back">${ICONS.back}</button>
      <span class="pane-title">Search messages</span></div>
      <div class="pane-view"><div class="search-box">${ICONS.search}
        <input type="text" id="cs-input" placeholder="Search history" maxlength="200" autocomplete="off">
      </div><div id="cs-results"></div></div>`;
    $("#cs-back").addEventListener("click", () => {
      clearTimeout(queryTimer); invalidateDetailsRead();
      Mesh.searchView = false; Mesh.searchQ = ""; Mesh.detailsKey = "";
      V.renderChatDetails();
    });
    const input = $("#cs-input");
    input.value = Mesh.searchQ || "";
    input.addEventListener("input", () => {
      clearTimeout(queryTimer); invalidateDetailsRead();
      Mesh.searchQ = input.value.trim();
      $("#cs-results").innerHTML = "";
      delete $("#cs-results").dataset.paint;
      queryTimer = setTimeout(() => {
        if (Mesh.searchView && Mesh.chatId === chatId && pane.dataset.collection === identity) {
          renderChatSearch("latest");
        }
      }, 300);
    });
    input.focus();
  }
  const q = (Mesh.searchQ || "").trim();
  if (q.length < 2) {
    delete $("#cs-results").dataset.paint;
    $("#cs-results").innerHTML = '<p class="hint">Enter at least two characters to search.</p>';
    return;
  }
  const data = await readCollection("search", mode);
  if (!data?.current()) return;
  const results = $("#cs-results");
  const paint = JSON.stringify(data);
  if (results.dataset.paint === paint) return;
  results.dataset.paint = paint;
  const scroll = pane.scrollTop;
  const windowStart = data.items?.[0]?.item_key || "";
  const resetScroll = results.dataset.windowStart !== windowStart;
  results.dataset.windowStart = windowStart;
  const mark = message => {
    const chars = Array.from(message.body || "");
    const first = Number.isInteger(message.match_start) ? message.match_start : 0;
    const last = Number.isInteger(message.match_end) ? message.match_end : first;
    const start = Math.max(0, first - 34);
    return (start ? "…" : "") + esc(chars.slice(start, first).join(""))
      + `<b>${esc(chars.slice(first, last).join(""))}</b>`
      + esc(chars.slice(last, last + 90).join(""));
  };
  results.innerHTML = (data.items || []).map(m => `<button class="search-hit" data-mid="${esc(m.id)}">
    <div class="sh-date">${esc(dayLabel(m.ts))} · ${esc(timeOnly(m.ts))}</div>
    <div class="sh-body">${esc(meshDn(m.from))}: ${mark(m)}</div></button>`).join("")
    + `<div class="page-history-controls">${collectionControls(data)}</div>`;
  pane.scrollTop = mode === "latest" || resetScroll ? 0 : scroll;
  results.querySelectorAll("[data-collection]").forEach(button => {
    button.addEventListener("click", () => renderChatSearch(button.dataset.collection));
  });
  results.querySelectorAll(".search-hit").forEach(button => {
    button.addEventListener("click", () => {
      clearTimeout(queryTimer); invalidateDetailsRead();
      Mesh.jumpTo = button.dataset.mid; Mesh.searchView = false; Mesh.searchQ = "";
      location.hash = `#/chats/${chatId}`;
    });
  });
}
V.renderChatSearch = renderChatSearch;
