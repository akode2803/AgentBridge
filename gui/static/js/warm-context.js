/* Session/lock-bound presentation context for a selected-room warm paint.

   This leaf owns no DOM, fetch or authority.  It deliberately copies only
   presentation fields; a fresh /api/mesh/chat response owns room content and
   membership. */

export const WARM_CONTEXT_MAX_AGE_MS = 5000;
export const WARM_CONTEXT_MAX_USERS = 2048;
export const WARM_CONTEXT_MAX_BYTES = 512 * 1024;
const USERNAME_RE = /^[a-z][a-z0-9_-]{1,31}$/;
const utf8Bytes = (value) => new TextEncoder().encode(value).byteLength;

function plainObject(value) {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

export function presentationFromState(state) {
  if (!plainObject(state) || typeof state.user !== "string"
      || !plainObject(state.users)) return null;
  const users = Object.create(null);
  const entries = Object.entries(state.users);
  if (entries.length > WARM_CONTEXT_MAX_USERS) return null;
  let retainedBytes = 0;
  for (const [name, value] of entries) {
    if (!USERNAME_RE.test(name) || !plainObject(value)) return null;
    const display = typeof value.display === "string" && value.display
      && value.display.length <= 256
      ? value.display : name;
    const item = { username: name, display };
    if (["human", "agent"].includes(value.display_kind)) {
      item.display_kind = value.display_kind;
    }
    if (plainObject(value.avatar)) {
      const avatar = {};
      if (typeof value.avatar.sha256 === "string"
          && value.avatar.sha256.length <= 128) avatar.sha256 = value.avatar.sha256;
      if (typeof value.avatar.updated === "string"
          && value.avatar.updated.length <= 64) avatar.updated = value.avatar.updated;
      if (Object.keys(avatar).length) item.avatar = Object.freeze(avatar);
    }
    if (typeof value.color === "string" && value.color.length <= 64) {
      item.color = value.color;
    }
    retainedBytes += utf8Bytes(name) + utf8Bytes(item.display)
      + utf8Bytes(item.color || "") + utf8Bytes(item.avatar?.sha256 || "")
      + utf8Bytes(item.avatar?.updated || "");
    if (retainedBytes > WARM_CONTEXT_MAX_BYTES) return null;
    users[name] = Object.freeze(item);
  }
  return Object.freeze({ user: state.user, users: Object.freeze(users) });
}

export function warmContext(snapshot, state, chatId, now = Date.now()) {
  if (!snapshot || !snapshot.bound || !plainObject(state) || !chatId
      || snapshot.exhausted || state.available !== true || state.restoring
      || snapshot.locked || !Number.isFinite(snapshot.ageMs) || snapshot.ageMs < 0
      || !Number.isSafeInteger(snapshot.stateGeneration)
      || snapshot.ageMs > WARM_CONTEXT_MAX_AGE_MS
      || state.user !== snapshot.viewer
      || ["offline", "restricted", "rate_limited", "auth_error",
          "permission_error", "configuration_error", "service_error"]
        .includes(state.connection?.state)
      || !Array.isArray(state.chats)
      || !state.chats.some((chat) => chat?.id === chatId)) return null;
  const presentation = presentationFromState(state);
  if (!presentation) return null;
  return Object.freeze({
    sessionEpoch: snapshot.sessionEpoch,
    lockEpoch: snapshot.lockEpoch,
    stateGeneration: snapshot.stateGeneration,
    acceptedAt: now - snapshot.ageMs,
    chatId,
    presentation,
  });
}

function sameBinding(left, right) {
  return plainObject(left) && plainObject(right)
    && typeof left.instance_id === "string" && left.instance_id
    && left.instance_id === right.instance_id
    && typeof left.viewer === "string" && left.viewer
    && left.viewer === right.viewer
    && typeof left.session_generation === "string" && left.session_generation
    && left.session_generation === right.session_generation;
}

export function initialChatContext(session, bootstrap, {
  state, snapshot, chatId, opening, details, restarting,
} = {}) {
  if (state != null || !opening || !chatId || details || restarting
      || !session || session.mode !== "bound" || !session.ready
      || session.exhausted || !plainObject(session.binding)
      || !snapshot || snapshot.locked || snapshot.exhausted
      || !Number.isSafeInteger(snapshot.sessionEpoch)
      || !Number.isSafeInteger(snapshot.lockEpoch)
      || !Number.isSafeInteger(snapshot.stateGeneration)
      || !plainObject(bootstrap) || bootstrap.configured !== true
      || bootstrap.restoring !== false || bootstrap.app_lock?.locked !== false
      || bootstrap.user !== session.binding.viewer
      || !sameBinding(session.binding, bootstrap.session_binding)) return null;
  const users = Object.freeze(Object.create(null));
  return Object.freeze({
    initial: true,
    presentation: Object.freeze({user: session.binding.viewer, users}),
  });
}

export function selectedChatContext(session, bootstrap, options = {}) {
  // A fresh canonical selected read supplies both membership and decorations.
  // An old sidebar is neither needed for first paint nor promoted as authority.
  return initialChatContext(session, bootstrap, { ...options, state: null });
}

export function sameWarmOperation(operation, current) {
  return !!operation && !!current
    && operation.sessionEpoch === current.sessionEpoch
    && operation.lockEpoch === current.lockEpoch
    && operation.stateGeneration === current.stateGeneration
    && operation.routeSeq === current.routeSeq
    && operation.chatId === current.chatId
    && operation.operationId === current.operationId
    && operation.chatRenderSeq === current.chatRenderSeq;
}
