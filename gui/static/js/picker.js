/* Shared multi-select picker surface (WhatsApp "Add member" / "Forward to").
   One row = avatar + name/sub + a big checkbox on the RIGHT, and an optional
   bottom bar that lists the chosen names comma-separated with a round confirm
   button. Consumers (members.js, forward.js) build their own section markup
   from pickerRow and wire the footer with bindPicker.

   Lives BELOW the page views (same layer as modal / composer) so both a view
   and another view can import it without a forbidden view→view dependency. */

import { esc } from "./util.js";
import { ICONS } from "./icons.js";

// value = the checkbox value the caller reads back; tag = optional badge
// (e.g. "agent"). Carries both `am-check` (legacy callers) and `pk-check`
// (footer binding) so existing selectors keep working.
export function pickerRow({ value, initial, name, sub, tag }) {
  return `
    <label class="mem-row modal-row pk-row">
      <span class="mem-avatar">${esc(((initial || "?")[0] || "?").toUpperCase())}</span>
      <span class="pk-text">
        <div class="mem-name">${esc(name)}${tag ? ` <span class="kind-tag">${esc(tag)}</span>` : ""}</div>
        ${sub ? `<div class="mem-sub">${esc(sub)}</div>` : ""}
      </span>
      <input type="checkbox" class="am-check pk-check" value="${esc(value)}">
    </label>`;
}

// a titled block of rows; empties collapse away
export function pickerSection(label, rowsHtml) {
  return rowsHtml ? `<div class="modal-sec">${esc(label)}</div>${rowsHtml}` : "";
}

// the WhatsApp send-bar. icon defaults to the send glyph (Forward); pass a
// check icon for the Add-member surface.
export function pickerFooter(icon) {
  return `
    <div class="picker-foot" hidden>
      <span class="pf-names"></span>
      <button class="pf-go primary" disabled>${icon || ICONS.send}</button>
    </div>`;
}

// keeps the footer's names + button state in sync with the checkboxes and
// invokes onConfirm(selectedValues) when the round button is clicked. Returns
// the sync fn so callers can re-run it after mutating the list.
export function bindPicker(box, onConfirm) {
  const foot = box.querySelector(".picker-foot");
  const names = box.querySelector(".pf-names");
  const go = box.querySelector(".pf-go");
  const sync = () => {
    const checked = [...box.querySelectorAll(".pk-check:checked")];
    if (foot) foot.hidden = checked.length === 0;
    if (names) names.textContent = checked
      .map((c) => c.closest(".mem-row").querySelector(".mem-name")
        .firstChild.textContent.trim())
      .join(", ");
    if (go) go.disabled = checked.length === 0;
  };
  box.querySelectorAll(".pk-check").forEach((c) => c.addEventListener("change", sync));
  if (go) go.addEventListener("click", () =>
    onConfirm([...box.querySelectorAll(".pk-check:checked")].map((c) => c.value)));
  sync();
  return sync;
}
