/* Small shared helpers: DOM shortcut, escaping, names, time/size formatting,
   toast. Leaf module — imports nothing. */

export const $ = (sel) => document.querySelector(sel);

export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Display names: role slugs are lowercase in the protocol; people aren't.
export function dn(role) {
  const known = { claude: "Claude", coco: "CoCo" };
  return known[role] || (role ? role[0].toUpperCase() + role.slice(1) : "");
}

export function fmtTime(tsUtc) {
  if (!tsUtc) return "never";
  const d = new Date(tsUtc);
  if (isNaN(d)) return tsUtc;
  const now = new Date();
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const sameDay = (a, b) => a.toDateString() === b.toDateString();
  if (sameDay(d, now)) return `Today ${time}`;
  const yest = new Date(now); yest.setDate(now.getDate() - 1);
  if (sameDay(d, yest)) return `Yesterday ${time}`;
  return d.toLocaleDateString([], { day: "numeric", month: "short" }) + " " + time;
}

export function timeOnly(tsUtc) {
  const d = new Date(tsUtc);
  return isNaN(d) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function dayLabel(tsUtc) {
  const d = new Date(tsUtc);
  if (isNaN(d)) return "";
  const now = new Date();
  const sameDay = (a, b) => a.toDateString() === b.toDateString();
  if (sameDay(d, now)) return "Today";
  const yest = new Date(now); yest.setDate(now.getDate() - 1);
  if (sameDay(d, yest)) return "Yesterday";
  return d.toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" });
}

export function fmtSize(bytes) {
  if (bytes == null) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

let toastTimer = null;
// toast(msg, true) = error (legacy form). toast(msg, {check, action,
// onAction, error, duration}) = snackbar with an optional ✓ and an action
// pill (e.g. Undo). When the info pane is open, snackbars dock inside it
// instead of covering the composer (WhatsApp behaviour, user-requested).
export function toast(msg, opts) {
  if (opts === true) opts = { error: true };
  opts = opts || {};
  const t = $("#toast");
  // opts.icon is trusted markup (our own ICONS / a glyph span), rendered
  // ahead of the message; opts.check is the plain ✓ success tick.
  const lead = opts.icon
    ? `<span class="toast-ic">${opts.icon}</span>`
    : (opts.check ? '<span class="toast-check">✓</span>' : "");
  t.innerHTML = lead +
    `<span class="toast-msg">${esc(msg)}</span>` +
    (opts.action ? `<button class="toast-act">${esc(opts.action)}</button>` : "");
  t.className = opts.error ? "error" : "";
  // dock over the LEFT sidebar when it's open and not collapsed
  // (WhatsApp; user corrected the earlier right-pane placement)
  const side = document.querySelector("#navrail");
  const sideVisible = side && side.offsetWidth > 60
    && !document.body.classList.contains("side-collapsed");
  if (sideVisible) {
    const r = side.getBoundingClientRect();
    t.style.left = (r.left + r.width / 2) + "px";
    t.classList.add("in-pane");
  } else {
    t.style.left = "";
  }
  t.hidden = false;
  const act = t.querySelector(".toast-act");
  if (act) act.addEventListener("click", () => {
    clearTimeout(toastTimer);
    t.hidden = true;
    if (opts.onAction) opts.onAction();
  });
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, opts.duration || 3600);
}

// the ≤1100px breakpoint puts the details pane on TOP of the chat — pane
// actions that need the chat visible (jump, reply) close the pane first
export function paneCoversChat() {
  return matchMedia("(max-width: 1100px)").matches;
}

// dismiss every floating menu so opening one closes the others (WhatsApp):
// the chat ⋮ dropdown, message + pin context menus, and the member-remove
// menu. Called at the top of each open path. (csel form popups manage their
// own open/close state, so they're left alone.)
export function closeMenus() {
  const cm = document.getElementById("chat-menu");
  if (cm) cm.hidden = true;
  document.querySelectorAll(".msg-menu, .mem-menu").forEach((m) => m.remove());
}

// "Read more" clamp for long messages. `store[mid]` is the line budget the
// message is currently allowed (undefined = the default `lines`; Infinity =
// fully expanded). The clamp height is snapped to a clean boundary so the cut
// never slices through the middle of a line, table or code block, and the
// "…Read more" link sits INSIDE the body at the end of the last visible line
// (WhatsApp/Instagram-style) rather than on a bar of its own. Expansion is
// remembered in `store` so the 2.5s re-renders don't re-collapse.
export function clampLong(scope, store, lines = 10) {
  scope.querySelectorAll(".msg-body").forEach((b) => {
    const mid = b.closest("[data-mid]")?.dataset.mid;
    if (!mid) return;
    const lh = parseFloat(getComputedStyle(b).lineHeight) || 20;
    const allowed = store[mid] || lines;
    const existing = b.querySelector(":scope > .read-more");
    const unclamp = () => {
      b.style.maxHeight = "";
      b.classList.remove("clamped");
      if (existing) existing.remove();
    };
    if (allowed === Infinity) { unclamp(); return; }
    const budget = allowed * lh;
    if (b.scrollHeight <= budget + 0.6) { unclamp(); return; }   // fits: no clamp
    b.style.maxHeight = cleanCut(b, budget, lh) + "px";
    b.classList.add("clamped");
    if (!existing) {
      const btn = document.createElement("button");
      btn.className = "read-more";
      btn.innerHTML = '<span class="rm-dots">…</span><span class="rm-label">Read more</span>';
      b.appendChild(btn);   // inside the body: it overlays the last line
    }
  });
}

// where to cut a clamped body so the last visible line/element is whole:
// walk the children, keeping those that fit entirely; a straddling text
// block is cut on a whole-line multiple, a straddling table/pre is left out
// (cut at the previous boundary) rather than sliced.
function cleanCut(body, budget, lh) {
  const top = body.getBoundingClientRect().top;
  const lineCut = Math.floor(budget / lh) * lh;   // whole body-lines within budget
  let cut = 0;
  for (const child of body.children) {
    if (child.classList.contains("read-more")) continue;
    const r = child.getBoundingClientRect();
    const cTop = r.top - top, cBot = r.bottom - top;
    if (cBot <= budget) { cut = cBot; continue; }   // whole child fits
    if (cTop >= budget) break;                       // child starts past budget
    if (/^(P|LI|UL|OL|H1|H2|H3|H4|DIV|BLOCKQUOTE)$/.test(child.tagName)) {
      const n = Math.floor((budget - cTop) / lh);    // whole lines within a text block
      if (n >= 1) cut = cTop + n * lh;
    } else if (child.tagName === "TABLE") {
      // show only WHOLE rows so a table is never sliced mid-row
      let rowCut = 0;
      child.querySelectorAll("tr").forEach((tr) => {
        const rb = tr.getBoundingClientRect().bottom - top;
        if (rb <= budget) rowCut = rb;
      });
      if (rowCut > cut) cut = rowCut;
      else if (budget - cut > 1.5 * lh) cut = Math.max(cut, lineCut);  // even row 1 too tall
    } else if (budget - cut > 1.5 * lh) {
      // pre / other non-sliceable taller than the budget: slice on a whole
      // body-line so each Read-more click keeps revealing more (no freeze)
      cut = Math.max(cut, lineCut);
    }   // else: element starts near the budget — snap cleanly before it
    break;
  }
  if (cut < lh) cut = Math.max(lh, lineCut);   // fallback
  return Math.round(cut);
}

// theme (basic dark mode; persisted, defaults to the OS preference)
export function initTheme() {
  const saved = localStorage.getItem("theme");
  if (saved) document.documentElement.dataset.theme = saved;
  else if (matchMedia("(prefers-color-scheme: dark)").matches) {
    document.documentElement.dataset.theme = "dark";
  }
}
export function setTheme(t) {
  document.documentElement.dataset.theme = t;
  localStorage.setItem("theme", t);
}
