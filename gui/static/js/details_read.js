/* Request ownership for every view sharing the details pane. */
import { Mesh, captureViewRead, viewReadMayApply } from "./state.js";

let active = null;
export function detailsIdentity() {
  return JSON.stringify([Mesh.chatId, !!Mesh.detailsView, !!Mesh.searchView,
    Mesh.searchQ || "", !!Mesh.mediaView, Mesh.mediaTab || "media",
    !!Mesh.starredPane, !!Mesh.agentsView, !!Mesh.permsView, Mesh.memberInfo || ""]);
}
export function beginDetailsRead() {
  const identity = detailsIdentity();
  if (active?.busy && active.identity === identity && detailsReadCurrent(active)) return null;
  const owner = captureViewRead();
  if (!Mesh.detailsView || !viewReadMayApply(owner)) return null;
  active = { owner, identity, busy: true };
  return active;
}
export function detailsReadCurrent(ticket, response) {
  return !!ticket && active === ticket && !!Mesh.detailsView
    && ticket.identity === detailsIdentity() && viewReadMayApply(ticket.owner, response);
}
export function finishDetailsRead(ticket) { if (ticket) ticket.busy = false; }
export function invalidateDetailsRead() { active = null; }
if (typeof document !== "undefined") {
  for (const event of ["ab:session-reset", "ab:lock-epoch"]) {
    document.addEventListener(event, invalidateDetailsRead);
  }
}
