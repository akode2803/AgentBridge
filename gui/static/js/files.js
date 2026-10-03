/* File-type helpers shared by the details pane and the media browser. */

export const IMG_EXTS = new Set(["png", "jpg", "jpeg", "gif", "webp", "svg"]);
export const isImg = (name) => IMG_EXTS.has((name || "").split(".").pop().toLowerCase());
// Message IDs position a fresh canonical read; they are never authorization.
export const fileUrl = (chatId, fileId, messageId) =>
  `/api/mesh/file?chat=${encodeURIComponent(chatId)}&id=${encodeURIComponent(fileId)}&message_id=${encodeURIComponent(messageId || "")}`;

export function monthLabel(ts) {
  const d = new Date(ts), now = new Date();
  if (isNaN(d)) return "";
  if (d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth()) {
    return "This month";
  }
  const m = d.toLocaleDateString([], { month: "long" });
  return d.getFullYear() === now.getFullYear() ? m : `${m} ${d.getFullYear()}`;
}

// A failed/pending byte response must not leave a permanent broken-image glyph.
// Retries are finite and belong to this DOM node, not to a retired chat route.
export function bindFilePreview(button) {
  const img = button.querySelector("img");
  if (!img || img._previewBound) return;
  img._previewBound = true;
  let attempts = 0, retry = null, label = null;
  const show = (text) => {
    if (!img.isConnected) return;
    if (!label) {
      label = document.createElement("span");
      label.className = "file-preview-label";
      button.appendChild(label);
    }
    label.textContent = text;
  };
  const cue = setTimeout(() => {
    if (!img.complete) show("Loading preview…");
  }, 500);
  const loaded = () => {
    clearTimeout(cue); clearTimeout(retry); retry = null;
    img.hidden = false; label?.remove(); label = null;
  };
  const failed = () => {
    clearTimeout(cue);
    if (!img.isConnected || retry !== null) return;
    img.hidden = true;
    if (attempts >= 3) { show("Preview unavailable · click to open"); return; }
    show("Loading preview…");
    retry = setTimeout(() => {
      retry = null;
      if (!img.isConnected) return;
      attempts += 1;
      img.loading = "eager";
      img.src = img.src;
    }, 500 * 2 ** attempts);
  };
  img.addEventListener("load", loaded);
  img.addEventListener("error", failed);
  if (img.complete) { if (img.naturalWidth) loaded(); else failed(); }
}
