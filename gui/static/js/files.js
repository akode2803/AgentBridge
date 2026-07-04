/* File-type helpers shared by the details pane and the media browser. */

import { esc, extIcon } from "./util.js";

export const IMG_EXTS = new Set(["png", "jpg", "jpeg", "gif", "webp", "svg"]);
export const isImg = (name) => IMG_EXTS.has((name || "").split(".").pop().toLowerCase());
export const fileUrl = (chatId, path) =>
  `/api/mesh/file?id=${encodeURIComponent(chatId)}&path=${encodeURIComponent(path)}`;

export function mediaThumb(chatId, f) {
  return isImg(f.name)
    ? `<span class="media-tile"><img src="${fileUrl(chatId, f.path)}" alt="" loading="lazy"></span>`
    : `<span class="media-tile file"><span style="font-size:19px">${extIcon(f.name)}</span>
       <span class="mt-ext">${esc((f.name.split(".").pop() || "").toUpperCase().slice(0, 5))}</span></span>`;
}

export function monthLabel(ts) {
  const d = new Date(ts), now = new Date();
  if (isNaN(d)) return "";
  if (d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth()) {
    return "This month";
  }
  const m = d.toLocaleDateString([], { month: "long" });
  return d.getFullYear() === now.getFullYear() ? m : `${m} ${d.getFullYear()}`;
}
