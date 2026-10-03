/* Deprecated broad polling is only a recovery path. A healthy event-first
   stream owns refresh; these timers never certify data or extend a session. */
export function createRefreshPolicy({refresh, healthy, background, connected = () => false,
  schedule = setTimeout, cancel = clearTimeout}) {
  let timer = null;
  let generation = 0;
  let stopped = true;
  function arm() {
    if (stopped || healthy() || timer !== null) return;
    const owner = generation;
    timer = schedule(async () => {
      timer = null;
      if (stopped || owner !== generation) return;
      // The stream may have opened after this fallback was scheduled.
      if (!healthy()) {
        try { await refresh(); } catch { /* bounded recovery on the next timer */ }
      }
      if (!stopped && owner === generation) arm();
    }, background() || connected() ? 20000 : 2500);
  }
  function changed() {
    generation += 1;
    if (timer !== null) cancel(timer);
    timer = null;
    arm();
  }
  return Object.freeze({
    start() { stopped = false; changed(); },
    changed,
    stop() { stopped = true; changed(); },
  });
}
