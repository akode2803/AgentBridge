/* One active expensive read and one replaceable pending desire. No result cache.
   Callers own session/route admission and recheck after awaiting the result. */
export function createLatestRead(delayMs = 150) {
  let active = false;
  let pending = null;
  let timer = null;

  function pump() {
    if (active || !pending) return;
    if (timer !== null) clearTimeout(timer);
    timer = setTimeout(async () => {
      timer = null;
      const wanted = pending;
      pending = null;
      if (!wanted) return;
      active = true;
      try {
        wanted.resolve(wanted.current() ? await wanted.read() : null);
      } catch {
        wanted.resolve(null);
      } finally {
        active = false;
        pump();
      }
    }, Math.max(0, pending.at + delayMs - performance.now()));
  }

  return Object.freeze({
    request(read, current) {
      if (pending) pending.resolve(null);
      return new Promise((resolve) => {
        pending = { read, current, resolve, at: performance.now() };
        pump();
      });
    },
    cancel() {
      if (timer !== null) clearTimeout(timer);
      timer = null;
      if (pending) pending.resolve(null);
      pending = null;
      // In-flight work keeps the slot until its bounded transport settles.
      // Its caller must reject the response after a session/route transition.
    },
  });
}
