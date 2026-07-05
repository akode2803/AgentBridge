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

export function extIcon(name) {
  const ext = (name || "").split(".").pop().toLowerCase();
  if (["csv", "xlsx", "xls"].includes(ext)) return "📊";
  if (["png", "jpg", "jpeg", "gif", "svg"].includes(ext)) return "🖼️";
  if (["md", "txt", "docx", "pdf"].includes(ext)) return "📄";
  return "📎";
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
  t.innerHTML = `${opts.check ? '<span class="toast-check">✓</span>' : ""}` +
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

// "Read more" clamp for long messages: anything taller than `lines` is cut
// to lines-1 (the Read-more line makes it `lines` total); every click on
// Read more grants `lines` more. Expansion is remembered in `store`
// (message id -> allowed lines), so the 2.5s re-renders don't re-collapse.
export function clampLong(scope, store, lines = 10) {
  scope.querySelectorAll(".msg-body").forEach((b) => {
    const mid = b.closest("[data-mid]")?.dataset.mid;
    if (!mid) return;
    const lh = parseFloat(getComputedStyle(b).lineHeight) || 20;
    const allowed = store[mid] || lines;
    const total = b.scrollHeight / lh;
    const existing = b.parentElement.querySelector(".read-more");
    if (total <= allowed + 0.6) {   // fits (with slack): no clamp
      b.style.maxHeight = "";
      b.classList.remove("clamped");
      if (existing) existing.remove();
      return;
    }
    b.style.maxHeight = ((allowed - 1) * lh) + "px";
    b.classList.add("clamped");
    if (!existing) {
      const btn = document.createElement("button");
      btn.className = "read-more";
      btn.innerHTML = '<span class="rm-dots">…</span><span class="rm-label">Read more</span>';
      b.after(btn);
    }
  });
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
