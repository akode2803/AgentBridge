"""Execute the production initial-route owner against browser-like history."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _initial_route_source() -> str:
    source = (ROOT / "gui" / "static" / "js" / "main.js").read_text(
        encoding="utf-8"
    )
    start = source.index("function routeInitialLocation()")
    return source[start : source.index("\n\n(async function start()", start)]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is unavailable")
def test_initial_route_canonicalizes_once_without_hashchange(tmp_path: Path):
    """Empty startup keeps query/state and invokes one route; deep links survive."""
    runner = tmp_path / "startup-route.mjs"
    runner.write_text(
        _RUNNER.replace("__SOURCE__", json.dumps(_initial_route_source())),
        encoding="utf-8",
    )
    completed = subprocess.run(
        ["node", str(runner)], text=True, capture_output=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


_RUNNER = r'''
import assert from "node:assert/strict";
const source = __SOURCE__;

function browser(initialHash) {
  let currentHash = initialHash;
  let hashchanges = 0;
  const location = {
    pathname: "/app/index.html",
    search: "?source=desktop",
  };
  Object.defineProperty(location, "hash", {
    get() { return currentHash; },
    set(value) { currentHash = value; hashchanges += 1; },
  });
  const originalState = {boot: 7};
  let routes = 0;
  const history = {
    state: originalState,
    replaceState(state, _title, url) {
      assert.equal(state, originalState);
      assert.equal(url, "#/chats");
      currentHash = url;
      // Browser replaceState does not dispatch hashchange.
    },
  };
  const route = () => { routes += 1; };
  const factory = new Function("location", "history", "route",
    `${source}; return routeInitialLocation;`);
  factory(location, history, route)();
  return {location, history, routes, hashchanges};
}

const empty = browser("");
assert.equal(empty.routes, 1);
assert.equal(empty.hashchanges, 0);
assert.equal(empty.location.hash, "#/chats");
assert.equal(empty.location.pathname, "/app/index.html");
assert.equal(empty.location.search, "?source=desktop");
assert.deepEqual(empty.history.state, {boot: 7});

const home = browser("#/chats");
assert.equal(home.routes, 1);
assert.equal(home.location.hash, "#/chats");

const deep = browser("#/chats/room-7/details");
assert.equal(deep.routes, 1);
assert.equal(deep.location.hash, "#/chats/room-7/details");
'''
