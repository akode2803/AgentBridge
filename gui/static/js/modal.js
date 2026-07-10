/* Modal infrastructure — centered dialog on desktop, full page on mobile
   (CSS switches). confirmModal replaces browser confirm(). */

import { esc } from "./util.js";
import { ICONS } from "./icons.js";

export function openModal(html) {
  closeModal();
  const scrim = document.createElement("div");
  scrim.className = "modal-scrim";
  scrim.innerHTML = `<div class="modal-box">${html}</div>`;
  scrim.addEventListener("mousedown", (e) => { if (e.target === scrim) closeModal(); });
  document.body.appendChild(scrim);
  return scrim.querySelector(".modal-box");
}

export function closeModal() {
  const m = document.querySelector(".modal-scrim");
  if (m) m.remove();
}

// Replace the OPEN modal's contents in place — no scrim teardown, so a
// multi-step flow (camera viewfinder → crop adjuster) transitions seamlessly
// instead of the scrim flashing out and back in. Falls back to openModal when
// nothing is open, so the same call opens fresh on the upload path (which has
// no modal yet) and reuses on the camera path. Returns the .modal-box either
// way; the className is reset so the caller re-applies its own step classes.
export function swapModal(html) {
  const box = document.querySelector(".modal-scrim > .modal-box");
  if (!box) return openModal(html);
  box.className = "modal-box";
  box.innerHTML = html;
  return box;
}

// WhatsApp-style confirmation dialog
export function confirmModal({ title, body, action = "Delete" }) {
  return new Promise((resolve) => {
    const box = openModal(`
      <div class="cf-title">${title}</div>
      <div class="cf-body">${esc(body)}</div>
      <div class="cf-actions">
        <button class="cf-cancel" id="cf-cancel">Cancel</button>
        <button class="cf-go" id="cf-go">${esc(action)}</button>
      </div>`);
    box.classList.add("confirm");
    box.parentElement.classList.add("confirm-scrim");
    box.querySelector("#cf-cancel").addEventListener("click", () => {
      closeModal(); resolve(false);
    });
    box.querySelector("#cf-go").addEventListener("click", () => {
      closeModal(); resolve(true);
    });
  });
}

// single-button acknowledgement dialog — same centered box + pill styling as
// confirmModal, but only an OK (nothing to decide). Resolves when dismissed.
export function alertModal({ title, body, action = "OK" }) {
  return new Promise((resolve) => {
    const box = openModal(`
      <div class="cf-title">${title}</div>
      <div class="cf-body">${esc(body)}</div>
      <div class="cf-actions">
        <button class="cf-pill" id="al-ok">${esc(action)}</button>
      </div>`);
    box.classList.add("confirm");
    box.parentElement.classList.add("confirm-scrim");
    box.querySelector("#al-ok").addEventListener("click", () => { closeModal(); resolve(); });
  });
}

// Profile-photo VIEWER (round E3, task 6/7): a minimal lightbox — dark scrim +
// the enlarged photo + the name + a close ✕. The photo FLIPs from its on-screen
// thumbnail (`origin`) to a centred square, then the name/close fade in; closing
// reverses the flight back into the thumbnail. `url` is the image src, `name`
// the caption, `origin` the avatar element to fly from.
export function openPhotoViewer(url, name, origin) {
  if (!url) return;
  document.querySelector(".photo-viewer")?.remove();   // one at a time
  const pv = document.createElement("div");
  pv.className = "photo-viewer";
  pv.innerHTML = `
    <div class="pv-backdrop"></div>
    <button class="pv-close" aria-label="Close">${ICONS.close}</button>
    <img class="pv-img" alt="" src="${esc(url)}">
    <div class="pv-name">${esc(name || "")}</div>`;
  document.body.appendChild(pv);
  const img = pv.querySelector(".pv-img");

  // map the (CSS-sized) centred target rect back onto the origin thumbnail, so
  // the first painted frame sits ON the thumbnail; the rAF release animates out.
  const flyTransform = () => {
    const o = origin?.getBoundingClientRect();
    const t = img.getBoundingClientRect();
    if (!o || !t.width) return null;
    const s = o.width / t.width;
    const dx = (o.left + o.width / 2) - (t.left + t.width / 2);
    const dy = (o.top + o.height / 2) - (t.top + t.height / 2);
    return `translate(${dx}px, ${dy}px) scale(${s})`;
  };
  const start = flyTransform();
  if (start) { img.style.transform = start; img.style.borderRadius = "50%"; }
  requestAnimationFrame(() => {
    pv.classList.add("open");
    img.style.transform = "";        // → identity: fly to centre
    img.style.borderRadius = "";
  });

  let closing = false;
  const close = () => {
    if (closing) return;
    closing = true;
    document.removeEventListener("keydown", onKey);
    const back = flyTransform();     // re-measure (window may have moved)
    pv.classList.remove("open");
    pv.classList.add("closing");
    if (back) { img.style.transform = back; img.style.borderRadius = "50%"; }
    const done = () => pv.remove();
    img.addEventListener("transitionend", done, { once: true });
    setTimeout(done, 320);           // fallback if transitionend is missed
  };
  pv.addEventListener("mousedown", (e) => {
    if (e.target === img) return;    // clicking the photo itself does nothing
    close();
  });
  // explicit click on ✕ too, so keyboard/assistive activation (which fires
  // click but not mousedown) still closes; the `closing` guard dedupes.
  pv.querySelector(".pv-close").addEventListener("click", close);
  const onKey = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
}

// live filter for modal lists: hides non-matching .modal-row items and any
// .modal-sec header whose section went empty
export function bindModalFilter(box) {
  const q = box.querySelector(".modal-q");
  if (!q) return;
  q.addEventListener("input", () => {
    const needle = q.value.trim().toLowerCase();
    box.querySelectorAll(".modal-row").forEach((r) => {
      r.hidden = !!needle && !r.textContent.toLowerCase().includes(needle);
    });
    box.querySelectorAll(".modal-sec").forEach((s) => {
      let el = s.nextElementSibling, any = false;
      while (el && !el.classList.contains("modal-sec")) {
        if (el.classList.contains("modal-row") && !el.hidden) any = true;
        el = el.nextElementSibling;
      }
      s.hidden = !any;
    });
  });
  q.focus();
}
