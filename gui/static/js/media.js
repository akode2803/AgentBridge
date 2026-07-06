/* Dedicated media browser (tabs: Media / Docs / Links, grouped by month).
   Renders into the details pane; renderChatDetails dispatches here. */

import { $, esc, fmtSize, fmtTime } from "./util.js";
import { ICONS, extIcon } from "./icons.js";
import { bindOpenFile } from "./api.js";
import { Mesh, meshDn } from "./state.js";
import { isImg, fileUrl, monthLabel } from "./files.js";
import { V } from "./views.js";

function renderChatMedia(data) {
  const chatId = Mesh.chatId;
  const tab = Mesh.mediaTab || "media";
  const media = data.files || [];
  const links = data.links || [];
  const items = tab === "media" ? media.filter((f) => isImg(f.name)).slice().reverse()
    : tab === "docs" ? media.filter((f) => !isImg(f.name)).slice().reverse()
    : links.slice().reverse();
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
      <button class="media-cell cd-file" data-path="${esc(f.path)}">
        <img src="${fileUrl(chatId, f.path)}" alt="${esc(f.name)}" loading="lazy">
      </button>`).join("")}</div>`,
    docs: (g) => g.items.map((f) => `
      <button class="att-btn cd-file" data-path="${esc(f.path)}"
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
    ? `<div class="empty" style="padding:30px 0">Nothing here yet</div>`
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
      <button class="icon-btn" id="cm-back">${ICONS.back}</button>
      <span class="pane-title">Media and files</span>
    </div>
    <div class="media-tabs">
      ${TABS.map((t) => `
        <button class="media-tab ${t === tab ? "active" : ""}" data-tab="${t}">
          ${t[0].toUpperCase() + t.slice(1)}</button>`).join("")}
      <span class="tab-ink" id="tab-ink"></span>
    </div>
    <div class="media-body ${dir ? "slide-" + dir : "pane-view"}">${body}</div>`;
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
  $("#cm-back").addEventListener("click", () => {
    Mesh.mediaView = false;
    Mesh._mediaPrev = null;
    Mesh._inkLeft = null;
    Mesh.detailsKey = "";
    V.renderChatDetails();
  });
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
