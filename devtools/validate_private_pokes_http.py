#!/usr/bin/env python3
"""Check the private poke channel against real Supabase Realtime.

docs/NAME_RETENTION_AND_PRIVATE_POKES.md makes ``ab-<root>`` private: only
active members of the root may join or send. This signs in the two Scratch
logins and joins real channels:

- both members of ``--root``: each joins, and a poke sent by one reaches
  the other;
- the reader on ``<root>-out`` (not a member) and ``<root>-rev`` (revoked):
  refused for authorization;
- the production ``_RealtimeThread`` (the transport's own session token):
  the owner's is ready on ``--root`` and woken by the reader's poke; the
  reader's is refused on ``<root>-out``;
- a public (non-private) join of ``ab-<root>``, last and on a connection of
  its own: refused once "Allow public access" is off; while it is on,
  whether it hears a private poke shows how old and new clients mix during
  a rollout.

The three roots are prepared on Scratch beforehand (owner member of all
three; reader member of ``--root``, absent from ``-out``, revoked in
``-rev``). The script prints user ids, channel states and booleans; never
passwords, tokens or keys.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

JOIN_TIMEOUT_S = 10.0
DELIVERY_TIMEOUT_S = 5.0


class GateFailure(RuntimeError):
    pass


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise GateFailure(f"required environment variable {name} is missing")
    return value


def _refused(state: str, error: str) -> bool:
    from agentbridge.transport.supabase import _authorization_refusal

    return state == "CHANNEL_ERROR" and _authorization_refusal(error)


class Login:
    """One signed-in async client; its token reaches Realtime on sign-in."""

    def __init__(self, url: str, key: str, email: str, password: str) -> None:
        self.url, self.key = url, key
        self.email, self.password = email, password
        self.client = None
        self.user_id = ""
        self.token = ""

    async def open(self) -> None:
        from supabase import acreate_client

        self.client = await acreate_client(self.url, self.key)
        response = await self.client.auth.sign_in_with_password({
            "email": self.email, "password": self.password})
        if response.user is None or response.session is None:
            raise GateFailure("a Scratch login did not sign in")
        self.user_id = str(response.user.id)
        self.token = response.session.access_token
        await self.client.realtime.set_auth(self.token)

    async def join(self, topic: str, *, private: bool = True):
        """Join ``topic``; return (channel, state, error, received pokes)."""
        states: asyncio.Queue = asyncio.Queue()
        received: asyncio.Queue = asyncio.Queue()
        channel = self.client.channel(topic, {"config": {
            "broadcast": {"self": False}, "private": private}})
        channel.on_broadcast("change", lambda payload: received.put_nowait(payload))

        def status(state, error=None):
            states.put_nowait((str(getattr(state, "value", state)), str(error or "")))

        await channel.subscribe(status)
        try:
            state, error = await asyncio.wait_for(states.get(), JOIN_TIMEOUT_S)
        except asyncio.TimeoutError:
            state, error = "TIMED_OUT", ""
        return channel, state, error, received

    async def close(self) -> None:
        if self.client is None:
            return
        try:
            await self.client.realtime.close()
        except Exception:  # noqa: BLE001 - shutdown is best effort
            pass
        try:
            await self.client.auth.close()
        except Exception:  # noqa: BLE001
            pass


async def _delivered(sender, queue: asyncio.Queue) -> bool:
    await sender.send_broadcast("change", {"r": 1})
    try:
        await asyncio.wait_for(queue.get(), DELIVERY_TIMEOUT_S)
        return True
    except asyncio.TimeoutError:
        return False


def _production_thread(url: str, key: str, root: str, token: str,
                       on_hint) -> Any:
    from agentbridge.transport.supabase import _RealtimeThread

    return _RealtimeThread(
        {"SUPABASE_URL": url, "SUPABASE_PUBLISHABLE_KEY": key}, root, on_hint,
        auth_mode="member:scratch", token_source=lambda: token)


def _wait(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


async def _check(root: str, url: str, key: str, owner: Login,
                 reader: Login, public: Login) -> dict[str, Any]:
    out: dict[str, Any] = {"mode": "pokes", "root": root}
    await owner.open()
    await reader.open()
    out["owner_id"], out["reader_id"] = owner.user_id, reader.user_id
    topic = f"ab-{root}"

    owner_ch, owner_state, owner_err, owner_got = await owner.join(topic)
    reader_ch, reader_state, reader_err, reader_got = await reader.join(topic)
    out["member_joins"] = {"owner": owner_state, "reader": reader_state}
    if owner_state != "SUBSCRIBED" or reader_state != "SUBSCRIBED":
        raise GateFailure(f"a member could not join: {out['member_joins']} "
                          f"{owner_err[:160]} {reader_err[:160]}")
    out["owner_to_reader"] = await _delivered(owner_ch, reader_got)
    out["reader_to_owner"] = await _delivered(reader_ch, owner_got)
    if not (out["owner_to_reader"] and out["reader_to_owner"]):
        raise GateFailure("a poke between members was not delivered")

    for suffix, label in (("-out", "outsider"), ("-rev", "revoked")):
        _ch, state, error, _got = await reader.join(f"ab-{root}{suffix}")
        out[f"{label}_join"] = {"state": state, "refused": _refused(state, error)}
        if not out[f"{label}_join"]["refused"]:
            raise GateFailure(f"the {label} reader was not refused: {state}")

    hints: list[int] = []
    ready = _production_thread(url, key, root, owner.token, lambda: hints.append(1))
    try:
        out["production_thread_ready"] = await asyncio.to_thread(
            _wait, lambda: ready.status() == "ready", JOIN_TIMEOUT_S)
        if out["production_thread_ready"]:
            await reader_ch.send_broadcast("change", {"r": 1})
            out["production_thread_woken"] = await asyncio.to_thread(
                _wait, lambda: bool(hints), DELIVERY_TIMEOUT_S)
    finally:
        ready.close()
    # The reader is not a member of "-out": the thread must record a refusal.
    refused = _production_thread(url, key, f"{root}-out", reader.token,
                                 lambda: None)
    try:
        await asyncio.to_thread(_wait, lambda: refused.status() == "disconnected",
                                JOIN_TIMEOUT_S)
        out["production_thread_outsider_refused"] = refused.refused()
    finally:
        refused.close()
    if not (out["production_thread_ready"]
            and out.get("production_thread_woken")
            and out["production_thread_outsider_refused"]):
        raise GateFailure(f"the production thread misbehaved: {out}")

    # Last, on a connection of its own: the client keeps one channel per
    # topic, so a public join of the same topic on the reader's connection
    # would replace its private channel.
    await public.open()
    _ch, public_state, _err, public_got = await public.join(topic, private=False)
    out["public_join"] = public_state
    if public_state == "SUBSCRIBED":
        out["public_hears_private"] = await _delivered(owner_ch, public_got)
    out["public_access_enforced"] = public_state != "SUBSCRIBED"
    out["result"] = "passed"
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    url = _required("AGENTBRIDGE_SCRATCH_SUPABASE_URL")
    key = _required("AGENTBRIDGE_SCRATCH_SUPABASE_PUBLISHABLE_KEY")
    owner = Login(url, key, _required("AGENTBRIDGE_SCRATCH_OWNER_EMAIL"),
                  _required("AGENTBRIDGE_SCRATCH_OWNER_PASSWORD"))
    reader_email = _required("AGENTBRIDGE_SCRATCH_READER_EMAIL")
    reader_password = _required("AGENTBRIDGE_SCRATCH_READER_PASSWORD")
    reader = Login(url, key, reader_email, reader_password)
    public = Login(url, key, reader_email, reader_password)

    async def run():
        try:
            return await _check(args.root, url, key, owner, reader, public)
        finally:
            for login in (owner, reader, public):
                await login.close()

    print(json.dumps(asyncio.run(run()), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GateFailure as exc:
        print(json.dumps({"result": "failed", "reason": str(exc)}))
        raise SystemExit(1) from exc
