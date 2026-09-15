/* Browser-local session response boundary.

   This leaf owns no cache or DOM.  The caller clears session-owned state after
   a transition decision and before applying the accepted payload. */

const MAX_GENERATION = 9223372036854775807n;
const GENERATION_RE = /^(0|[1-9][0-9]*)$/;

export function parseSessionBinding(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const keys = Object.keys(value).sort();
  if (keys.join(",") !== "instance_id,session_generation,viewer") return null;
  const instanceId = value.instance_id;
  const generation = value.session_generation;
  const viewer = value.viewer;
  if (typeof instanceId !== "string" || !instanceId || instanceId.length > 128
      || instanceId.includes("\0")) return null;
  if (viewer !== null && (typeof viewer !== "string" || !viewer
      || viewer.length > 128 || viewer.includes("\0"))) return null;
  if (typeof generation !== "string" || generation.length > 19
      || !GENERATION_RE.test(generation)) return null;
  let numeric;
  try { numeric = BigInt(generation); } catch { return null; }
  if (numeric > MAX_GENERATION) return null;
  return Object.freeze({
    instance_id: instanceId,
    session_generation: generation,
    viewer,
    _generation: numeric,
  });
}

function publicBinding(binding) {
  if (!binding) return null;
  return Object.freeze({
    instance_id: binding.instance_id,
    session_generation: binding.session_generation,
    viewer: binding.viewer,
  });
}

function sameBinding(a, b) {
  return !!a && !!b && a.instance_id === b.instance_id
    && a.session_generation === b.session_generation && a.viewer === b.viewer;
}

function legacyBootstrap(payload) {
  const caps = payload?.caps;
  if (caps !== undefined
      && (!caps || typeof caps !== "object" || Array.isArray(caps))) return false;
  if (!payload || typeof payload !== "object" || Array.isArray(payload)
      || "session_binding" in payload
      || (caps && "session_binding_v1" in caps)) return false;
  // The shared assets can be re-read from disk by a still-running server.  Admit
  // only the two server shapes known to predate this protocol: the old bridge
  // (no `v`) and recognized unbound v2 releases through 0.24.272. This mode carries no R188
  // guarantee and cannot be entered after a bound bootstrap was accepted.
  if (typeof payload.configured === "boolean" && !("v" in payload)
      && typeof payload.gui_version === "string"
      && typeof payload.bridge_version === "string") return true;
  const match = typeof payload.gui_version === "string"
    ? /^0\.24\.(\d+)$/.exec(payload.gui_version) : null;
  return payload.v === 2 && typeof payload.configured === "boolean"
    && typeof payload.instance_id === "string" && payload.instance_id
    && (payload.user === null || typeof payload.user === "string")
    && !!match && Number(match[1]) <= 272;
}

export function createSessionBoundary({ maxRetiredInstances = 64 } = {}) {
  if (!Number.isInteger(maxRetiredInstances) || maxRetiredInstances < 1) {
    throw new TypeError("maxRetiredInstances must be a positive integer");
  }
  let epoch = 0;
  let latestSequence = 0;
  let adoptedSequence = 0;
  let mode = "undecided";
  let binding = null;
  let generationFloor = null;
  let expectedInstance = null;
  let exhausted = false;
  let ready = false;
  const retired = new Set();

  const validEpochTicket = (ticket) => !!ticket && typeof ticket === "object"
    && Number.isSafeInteger(ticket.epoch) && ticket.epoch === epoch;

  function capture() {
    return Object.freeze({ epoch });
  }

  function beginBootstrap() {
    if (latestSequence >= Number.MAX_SAFE_INTEGER) exhausted = true;
    else latestSequence += 1;
    return Object.freeze({ epoch, sequence: latestSequence });
  }

  function invalidate() {
    if (epoch >= Number.MAX_SAFE_INTEGER) exhausted = true;
    else epoch += 1;
    expectedInstance = null;
    binding = null;
    ready = false;
    adoptedSequence = 0;
    return Object.freeze({ transition: true });
  }

  function retire(instanceId) {
    retired.add(instanceId);
    if (retired.size > maxRetiredInstances) exhausted = true;
  }

  function reject(reason, transition = false) {
    return Object.freeze({ accepted: false, transition, reason });
  }

  function acceptBootstrap(ticket, payload) {
    if (exhausted) return reject("exhausted", true);
    if (!validEpochTicket(ticket) || !Number.isSafeInteger(ticket.sequence)
        || ticket.sequence !== latestSequence || ticket.sequence <= adoptedSequence) {
      return reject("stale_bootstrap");
    }
    const capability = payload?.caps?.session_binding_v1 === true;
    if (!capability) {
      if ((mode === "undecided" || mode === "legacy") && legacyBootstrap(payload)) {
        mode = "legacy";
        ready = true;
        adoptedSequence = ticket.sequence;
        return Object.freeze({ accepted: true, transition: false,
          binding: null, mode });
      }
      return reject("binding_capability_missing");
    }
    const candidate = parseSessionBinding(payload.session_binding);
    if (!candidate || payload.v !== 2) return reject("malformed_binding");
    if (payload.instance_id !== candidate.instance_id
        || payload.user !== candidate.viewer) return reject("inconsistent_binding");
    if (retired.has(candidate.instance_id)) return reject("retired_instance");
    if (expectedInstance && candidate.instance_id !== expectedInstance) {
      return reject("unexpected_instance");
    }
    if (!binding) {
      if (generationFloor && candidate.instance_id !== generationFloor.instance_id
          && !expectedInstance) {
        retire(generationFloor.instance_id);
        if (exhausted) return reject("exhausted", true);
        expectedInstance = candidate.instance_id;
        if (epoch >= Number.MAX_SAFE_INTEGER) {
          exhausted = true;
          return reject("exhausted", true);
        }
        epoch += 1;
        ready = false;
        adoptedSequence = 0;
        return Object.freeze({ accepted: false, transition: true,
          retryInstance: expectedInstance });
      }
      if (generationFloor && candidate.instance_id === generationFloor.instance_id) {
        if (candidate._generation < generationFloor._generation) {
          return reject("generation_rollback");
        }
        if (candidate._generation === generationFloor._generation
            && candidate.viewer !== generationFloor.viewer) {
          return reject("viewer_mismatch");
        }
      }
      const transition = mode === "legacy";
      if (transition) {
        if (epoch >= Number.MAX_SAFE_INTEGER) {
          exhausted = true;
          return reject("exhausted", true);
        }
        epoch += 1;
      }
      binding = candidate;
      generationFloor = candidate;
      expectedInstance = null;
      mode = "bound";
      ready = true;
      adoptedSequence = ticket.sequence;
      return Object.freeze({ accepted: true, transition,
        binding: publicBinding(binding), mode });
    }
    if (candidate.instance_id !== binding.instance_id) {
      retire(binding.instance_id);
      if (exhausted) return reject("exhausted", true);
      binding = null;
      ready = false;
      expectedInstance = candidate.instance_id;
      if (epoch >= Number.MAX_SAFE_INTEGER) {
        exhausted = true;
        return reject("exhausted", true);
      }
      epoch += 1;
      adoptedSequence = 0;
      return Object.freeze({ accepted: false, transition: true,
        retryInstance: expectedInstance });
    }
    if (candidate._generation < binding._generation) {
      return reject("generation_rollback");
    }
    if (candidate._generation === binding._generation
        && candidate.viewer !== binding.viewer) {
      return reject("viewer_mismatch");
    }
    const transition = candidate._generation > binding._generation;
    if (transition) {
      if (epoch >= Number.MAX_SAFE_INTEGER) {
        exhausted = true;
        return reject("exhausted", true);
      }
      epoch += 1;
      binding = candidate;
      generationFloor = candidate;
    } else if (!sameBinding(binding, candidate)) {
      return reject("binding_mismatch");
    }
    mode = "bound";
    ready = true;
    adoptedSequence = ticket.sequence;
    return Object.freeze({ accepted: true, transition,
      binding: publicBinding(binding), mode });
  }

  function mayApply(ticket, source) {
    if (exhausted || !validEpochTicket(ticket)) return false;
    if (source === undefined) return ready;
    if (mode !== "bound" || !binding) return mode === "legacy" && ready;
    const raw = source && typeof source === "object" && "session_binding" in source
      ? source.session_binding : source;
    return sameBinding(binding, parseSessionBinding(raw));
  }

  function snapshot() {
    return Object.freeze({ epoch, mode, binding: publicBinding(binding),
      expectedInstance, retiredCount: retired.size, exhausted, ready, latestSequence });
  }

  return Object.freeze({ capture, beginBootstrap, invalidate,
    acceptBootstrap, mayApply, snapshot });
}

export const BrowserSession = createSessionBoundary();
