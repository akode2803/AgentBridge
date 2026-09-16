/* Regional, delayed feedback. No request scheduling or artificial minimum wait. */
const pending = new WeakMap();

export function endLoading(host) {
  pending.get(host)?.();
}

export function beginLoading(host, { label = "Loading…", current = () => true, placement = "corner" } = {}) {
  if (!host) return () => {};
  endLoading(host);
  let status = null;
  const previousBusy = host.getAttribute("aria-busy");
  const finish = () => {
    clearTimeout(timer);
    status?.remove();
    if (pending.get(host) !== finish) return;
    pending.delete(host);
    host.classList.remove("loading-host");
    if (previousBusy === null) host.removeAttribute("aria-busy");
    else host.setAttribute("aria-busy", previousBusy);
  };
  const timer = setTimeout(() => {
    if (!host.isConnected || !current()) { finish(); return; }
    if (document.querySelector("#boot:not(.done), #connecting, #lock, #auth")) {
      finish(); return; // A full-page cover already owns progress feedback.
    }
    const welcome = placement === "center" ? host.querySelector(".empty-state .es-box") : null;
    status = document.createElement("span");
    status.className = "loading-status" + (welcome ? " loading-inline" : placement === "center" ? " loading-centered" : "");
    status.setAttribute("role", "status");
    status.innerHTML = '<span class="spin-sm" aria-hidden="true"></span>';
    const text = document.createElement("span");
    text.textContent = label;
    status.appendChild(text);
    host.classList.add("loading-host");
    host.setAttribute("aria-busy", "true");
    (welcome || host).appendChild(status);
  }, 500);
  pending.set(host, finish);
  return finish;
}
