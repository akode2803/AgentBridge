/* Modal infrastructure — centered dialog on desktop, full page on mobile
   (CSS switches). confirmModal replaces browser confirm(). */

import { esc } from "./util.js";

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
