/* One delayed, list-shaped cue. Retained chat rows never need a refresh banner. */
let active = null;

export function clearSidebarProgress() {
  if (!active) return;
  clearTimeout(active.timer); clearTimeout(active.exit);
  active.row?.remove();
  active.host.classList.remove("sidebar-results-arrive");
  active = null;
}

export function syncSidebarProgress(host, {pending = false, limited = false, current = () => true} = {}) {
  if (!host) return;
  if (active && active.host !== host) clearSidebarProgress();
  if (!active) active = {host, row:null, timer:null, exit:null, current};
  const state = active;
  state.current = current;
  state.limited = limited;
  if (!current()) { clearSidebarProgress(); return; }
  const show = () => {
    state.timer = null;
    if (active !== state || !host.isConnected || !state.current()) return;
    if (document.querySelector("#boot:not(.done), #connecting, #lock, #auth")) return;
    if (!state.row) {
      state.row = document.createElement("div");
      state.row.className = "sidebar-progress";
      state.row.setAttribute("role", "status");
    }
    const kind = state.limited ? "limited" : "pending";
    if (state.kind !== kind) state.row.innerHTML = `<div class="sidebar-progress-row">
      <span class="sidebar-progress-avatar" aria-hidden="true">${state.limited ? "!" : '<span class="spin-sm"></span>'}</span>
      <span class="chat-mid"><span class="chat-name">${state.limited ? "Chat list limit reached" : "Updating chats…"}</span>
        <span class="chat-last">${state.limited ? "Some chats exceed the current loading limit." : "Your chats will appear here."}</span></span></div>`;
    state.kind = kind;
    if (state.row.parentNode !== host) host.prepend(state.row);
  };
  if (pending || limited) {
    clearTimeout(state.exit); state.exit = null;
    host.classList.remove("sidebar-results-arrive");
    state.row?.classList.remove("is-leaving");
    if (state.row || limited) { clearTimeout(state.timer); show(); }
    else if (state.timer === null) state.timer = setTimeout(show, 500);
    return;
  }
  clearTimeout(state.timer); state.timer = null;
  if (!state.row || state.exit !== null) return;
  state.row.classList.add("is-leaving");
  host.classList.add("sidebar-results-arrive");
  state.exit = setTimeout(() => {
    if (active !== state) return;
    state.row?.remove(); state.row = null; state.kind = null; state.exit = null;
    host.classList.remove("sidebar-results-arrive");
  }, 200);
}

if (typeof document !== "undefined") {
  for (const event of ["ab:session-reset", "ab:lock-epoch"]) {
    document.addEventListener(event, clearSidebarProgress);
  }
}
