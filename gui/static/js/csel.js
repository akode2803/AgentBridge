/* Custom dropdown — native <select> popups ignore theming and clip inside
   scrolling panes, so options render in a fixed-position popup on body. */

import { esc } from "./util.js";

export function csel({ options, value, onChange }) {
  const root = document.createElement("div");
  root.className = "csel";
  root.dataset.value = value ?? "";
  const label = () =>
    (options.find((o) => String(o.v) === String(root.dataset.value)) || options[0]).label;
  const btn = document.createElement("button");
  btn.type = "button";
  btn.innerHTML = `<span class="csel-value"></span><span class="csel-caret">▾</span>`;
  btn.querySelector(".csel-value").textContent = label();
  root.appendChild(btn);
  let pop = null;
  const close = () => {
    if (pop) { pop.remove(); pop = null; }
    document.removeEventListener("mousedown", away, true);
  };
  const away = (e) => {
    if (!root.contains(e.target) && !(pop && pop.contains(e.target))) close();
  };
  btn.addEventListener("click", () => {
    if (pop) { close(); return; }
    pop = document.createElement("div");
    pop.className = "csel-pop";
    pop.innerHTML = options.map((o) => `
      <button type="button" class="csel-opt ${String(o.v) === String(root.dataset.value) ? "sel" : ""}"
              data-v="${esc(o.v)}">${esc(o.label)}</button>`).join("");
    document.body.appendChild(pop);
    const r = btn.getBoundingClientRect();
    pop.style.minWidth = Math.max(180, r.width) + "px";
    const ph = pop.offsetHeight, pw = pop.offsetWidth;
    const above = r.bottom + ph + 8 > innerHeight;
    if (above) pop.classList.add("above");   // slide up, not down
    pop.style.top = Math.max(8, above ? r.top - ph - 4 : r.bottom + 4) + "px";
    pop.style.left = Math.max(8, Math.min(r.left, innerWidth - pw - 8)) + "px";
    pop.querySelectorAll(".csel-opt").forEach((ob) => {
      ob.addEventListener("click", () => {
        root.dataset.value = ob.dataset.v;
        btn.querySelector(".csel-value").textContent = label();
        close();
        if (onChange) onChange(ob.dataset.v);
      });
    });
    document.addEventListener("mousedown", away, true);
  });
  return root;
}

// mount a csel into every placeholder <div class="csel-slot" …>
export function mountCsels(scope, options, onChange) {
  scope.querySelectorAll(".csel-slot").forEach((slot) => {
    const el = csel({
      options: typeof options === "function" ? options(slot) : options,
      value: slot.dataset.value || "",
      onChange: (v) => { slot.dataset.value = v; if (onChange) onChange(slot, v); },
    });
    slot.appendChild(el);
  });
}
