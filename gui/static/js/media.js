/* Dedicated media browser (tabs: Media / Docs / Links, grouped by month).
   Renders into the details pane; renderChatDetails dispatches here. */

import { $, esc, fmtSize, fmtTime } from "./util.js";
import { ICONS, extIcon } from "./icons.js";
import { bindOpenFile } from "./api.js";
import { Mesh, meshDn } from "./state.js";
import { fileUrl, monthLabel } from "./files.js";
import { readCollection, collectionControls, pendingDetails } from "./detail_pages.js";
import { V } from "./views.js";

async function renderChatMedia(mode = "refresh") {
  const chatId = Mesh.chatId;
  const tab = Mesh.mediaTab || "media";
  const pane = $("#details-pane");
  const back = () => { Mesh.mediaView = false; Mesh.detailsKey = ""; V.renderChatDetails(); };
  const identity = JSON.stringify([chatId, "media", tab]);
  if (pane.dataset.collection !== identity || !pane.querySelector(".pane-head")) {
    pendingDetails("Media and files", back);
    pane.dataset.collection = identity;
    pane.dataset.collectionPaint = "";
  }
  const data = await readCollection(tab, mode);
  if (!data?.current()) return;
  const paint = JSON.stringify(data);
  if (pane.dataset.collectionPaint === paint) return;
  pane.dataset.collectionPaint = paint;
  const scroll = pane.scrollTop;
  const items = data.items || [];
  const windowStart = items[0]?.item_key || "";
  const resetScroll = pane.dataset.collectionWindow !== windowStart;
  pane.dataset.collectionWindow = windowStart;
  const groups = [];
  for (const it of items) {
    const label = monthLabel(it.ts);
    if (!groups.length || groups[groups.length - 1].label !== label) {
      groups.push({ label, items: [] });
    }
    groups[groups.length - 1].items.push(it);
  }
  const render = {
    media: (g) => `<div class="media-grid">${g.items.map((f) => `
      <button class="media-cell cd-file" data-id="${esc(f.id)}" data-message-id="${esc(f.msg_id)}" data-name="${esc(f.name)}">
        <img src="${fileUrl(chatId, f.id, f.msg_id)}" alt="${esc(f.name)}" loading="lazy">
      </button>`).join("")}</div>`,
    docs: (g) => g.items.map((f) => `
      <button class="att-btn cd-file" data-id="${esc(f.id)}" data-message-id="${esc(f.msg_id)}" data-name="${esc(f.name)}"
              style="max-width:100%;margin-top:6px">
        <span class="att-icon">${extIcon(f.name)}</span>
        <span style="min-width:0">
          <div class="att-name">${esc(f.name)}</div>
          <div class="att-size">${fmtSize(f.bytes)} · ${esc(meshDn(f.from))} · ${esc(fmtTime(f.ts))}</div>
        </span>
      </button>`).join(""),
    links: (g) => g.items.map((l) => `
      <div class="link-row">
        <span class="att-icon">🔗</span>
        <span style="min-width:0">
          <div><a href="${esc(l.url)}" target="_blank" rel="noopener">${
            esc(l.url.length > 58 ? l.url.slice(0, 58) + "…" : l.url)}</a></div>
          <div class="att-size">${esc(meshDn(l.from))} · ${esc(fmtTime(l.ts))}</div>
        </span>
      </div>`).join(""),
  };
  const body = !items.length
    ? ""
    : groups.map((g) => `
        <div class="media-month">${esc(g.label)}</div>${render[tab](g)}`).join("");
  // tab switches animate: the underline glides between tabs and the body
  // slides in from the direction of travel
  const TABS = ["media", "docs", "links"];
  const prev = Mesh._mediaPrev;
  const dir = prev && prev !== tab
    ? (TABS.indexOf(tab) > TABS.indexOf(prev) ? "r" : "l") : "";
  Mesh._mediaPrev = tab;
  $("#details-pane").innerHTML = `
    <div class="pane-head">
      <button class="icon-btn" id="cm-back" aria-label="Back">${ICONS.back}</button>
      <span class="pane-title">Media and files</span>
    </div>
    <div class="media-tabs">
      ${TABS.map((t) => `
        <button class="media-tab ${t === tab ? "active" : ""}" data-tab="${t}">
          ${t[0].toUpperCase() + t.slice(1)}</button>`).join("")}
      <span class="tab-ink" id="tab-ink"></span>
    </div>
    <div class="media-body ${dir ? "slide-" + dir : "pane-view"}">${body}<div class="page-history-controls">${collectionControls(data)}</div></div>`;
  const act = document.querySelector(".media-tab.active");
  const ink = $("#tab-ink");
  const place = () => {
    ink.style.left = act.offsetLeft + "px";
    ink.style.width = act.offsetWidth + "px";
  };
  if (Mesh._inkLeft != null && dir) {
    ink.style.left = Mesh._inkLeft + "px";       // start where it was…
    ink.style.width = Mesh._inkW + "px";
    requestAnimationFrame(() => requestAnimationFrame(place));  // …glide over
  } else {
    place();
  }
  Mesh._inkLeft = act.offsetLeft;
  Mesh._inkW = act.offsetWidth;
  $("#cm-back").addEventListener("click", back);
  pane.querySelectorAll("[data-collection]").forEach(button => {
    button.addEventListener("click", () => renderChatMedia(button.dataset.collection));
  });
  pane.scrollTop = mode === "latest" || resetScroll ? 0 : scroll;
  document.querySelectorAll(".media-tab").forEach((b) => {
    b.addEventListener("click", () => {
      if (b.dataset.tab === Mesh.mediaTab) return;
      Mesh.mediaTab = b.dataset.tab;
      Mesh.detailsKey = "";
      V.renderChatDetails();
    });
  });
  bindOpenFile($("#details-pane"), chatId, ".cd-file");
}
V.renderChatMedia = renderChatMedia;
