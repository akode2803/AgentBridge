/* In-chat message search (WhatsApp-style results: date, sender, snippet).
   Fetches the transcript itself, on demand — chat info no longer hauls
   1000 messages around. */

import { $, esc, timeOnly, dayLabel, toast } from "./util.js";
import { ICONS } from "./icons.js";
import { api } from "./api.js";
import { Mesh, meshDn } from "./state.js";
import { V } from "./views.js";

async function renderChatSearch() {
  const chatId = Mesh.chatId;
  const data = await api(`/api/mesh/chat?id=${encodeURIComponent(chatId)}&tail=1000`);
  if (data.error) { toast(data.error, true); return; }
  $("#details-pane").innerHTML = `
    <div class="pane-head">
      <button class="icon-btn" id="cs-back">${ICONS.back}</button>
      <span class="pane-title">Search messages</span>
    </div>
    <div class="pane-view">
      <div class="search-box">${ICONS.search}
        <input type="text" id="cs-input" placeholder="Search" autocomplete="off">
      </div>
      <div id="cs-results"></div>
    </div>`;
  $("#cs-back").addEventListener("click", () => {
    Mesh.searchView = false;
    Mesh.searchQ = "";
    Mesh.detailsKey = "";
    V.renderChatDetails();
  });
  const input = $("#cs-input");
  const results = $("#cs-results");
  const run = () => {
    const q = input.value.trim();
    Mesh.searchQ = q;
    if (q.length < 2) { results.innerHTML = ""; return; }
    const ql = q.toLowerCase();
    const hits = data.messages
      .filter((m) => (m.body || "").toLowerCase().includes(ql))
      .slice(-50).reverse();
    const mark = (body) => {
      const i = body.toLowerCase().indexOf(ql);
      const start = Math.max(0, i - 34);
      const snip = (start > 0 ? "…" : "") + body.slice(start, i + ql.length + 90);
      return esc(snip).replace(new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "i"),
        (mm) => `<b>${mm}</b>`);
    };
    results.innerHTML = hits.map((m) => `
      <button class="search-hit" data-mid="${esc(m.id || "")}">
        <div class="sh-date">${esc(dayLabel(m.ts))} · ${esc(timeOnly(m.ts))}</div>
        <div class="sh-body">${esc(meshDn(m.from))}: ${mark(m.body || "")}</div>
      </button>`).join("") ||
      `<div class="empty" style="padding:26px 0">No messages found</div>`;
    results.querySelectorAll(".search-hit").forEach((b) => {
      b.addEventListener("click", () => {
        Mesh.jumpTo = b.dataset.mid;
        Mesh.searchView = false;
        Mesh.searchQ = "";
        location.hash = `#/chats/${chatId}`;   // close info, land on the message
      });
    });
  };
  input.addEventListener("input", run);
  input.value = Mesh.searchQ || "";
  if (input.value) run();
  input.focus();
}
V.renderChatSearch = renderChatSearch;
