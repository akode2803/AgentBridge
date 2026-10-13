"""Typed self-claim of a username on a sealed Supabase root.

A sealed root refuses the legacy direct ``ab_members`` insert, so ``join``
claims through ``ab_mutation_member_claim``. The operation key derives from the
root, the authenticated account and the username, so every attempt at one claim
(a retry, or a later rerun of ``join``) replays a single exact operation: a lost
reply never claims twice, and its receipt is always recoverable for
acknowledgment. An unacknowledged receipt blocks epoch rotation, so a rerun
observes the epoch that holds it. No local journal ticket is involved: the
installed member credential fixes the account and the key is derived again.
"""

from __future__ import annotations

import json
import time
from uuid import NAMESPACE_URL, uuid5

from .receipt_retirement import observe_provider_epoch
from .typed_mutations import TypedMutationRPC, TypedMutationRPCError, prepare_mutation

__all__ = [
    "ALREADY_MEMBER", "CLAIMED", "MemberClaimError", "UNSEALED",
    "claim_member", "member_claim_operation_id", "open_member_rpc",
]

CLAIMED = "claimed"
ALREADY_MEMBER = "already_member"
UNSEALED = "unsealed"

# Provider replies meaning the typed lifecycle is absent: an unsealed root
# (55000) or a schema without the epoch RPC.
_UNSEALED_CODES = frozenset(("55000", "PGRST202", "42883"))


class MemberClaimError(RuntimeError):
    """``reason``: taken, full, refused or unknown (rerun to replay)."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def member_claim_operation_id(root: str, actor_id: str, username: str) -> str:
    key = json.dumps(["agentbridge-member-claim-v1", root, actor_id, username],
                     separators=(",", ":"), ensure_ascii=False)
    return str(uuid5(NAMESPACE_URL, key))


def open_member_rpc(env: dict[str, str], root: str, actor_id: str) -> TypedMutationRPC:
    """Sign in with the member credential in ``env`` and pin it for typed RPCs."""
    from ..node.protocol import canonical_endpoint
    from .supabase import SupabaseTransport

    provider = SupabaseTransport(root, env=env)
    return TypedMutationRPC(provider, actor_id=actor_id, root=root,
                            endpoint=canonical_endpoint(env.get("SUPABASE_URL", "")))


def _epoch(rpc: TypedMutationRPC) -> str:
    try:
        return observe_provider_epoch(rpc).protocol_epoch
    except TypedMutationRPCError:
        raise MemberClaimError("unknown", "could not read the root's mutation epoch") from None


def _own_username(rpc: TypedMutationRPC) -> str | None:
    """This account's root username, through the pinned authenticated client."""
    try:
        rows = (rpc._postgrest.from_("ab_members").select("username")
                .eq("root", rpc.root).eq("uid", rpc.actor_id).execute().data)
    except Exception:  # noqa: BLE001 - an unreadable row is not membership
        raise MemberClaimError("unknown", "could not read this account's membership") from None
    rpc._check_identity()
    if type(rows) is list and len(rows) == 1 and type(rows[0]) is dict:
        name = rows[0].get("username")
        return name if type(name) is str else None
    return None


def _retry_delay(attempt: int) -> float:
    return min(2.0, 0.25 * 2 ** attempt)


def claim_member(rpc: TypedMutationRPC, username: str, *, attempts: int = 4,
                 sleep=time.sleep) -> str:
    """Claim ``username`` for the RPC's account on its root.

    Returns ``claimed`` once the receipt is acknowledged, ``already_member``
    when this account already holds the name, or ``unsealed`` when the root
    still takes legacy writes (the caller claims the legacy way). Raises
    MemberClaimError otherwise; ``unknown`` is safe to rerun.
    """
    try:
        epoch = observe_provider_epoch(rpc).protocol_epoch
    except TypedMutationRPCError as exc:
        if exc.code in _UNSEALED_CODES:
            return UNSEALED
        raise MemberClaimError("unknown", "could not read the root's mutation epoch") from None
    operation = member_claim_operation_id(rpc.root, rpc.actor_id, username)
    for attempt in range(attempts):
        request = prepare_mutation(rpc.root, epoch, operation, rpc.actor_id,
                                   "member_claim", {"p_username": username})
        try:
            receipt = rpc.execute(request)
        except TypedMutationRPCError as exc:
            if exc.code == "54000":
                raise MemberClaimError(
                    "full", "the root's receipt epoch is full; an operator must "
                    "rotate it before anyone can join") from None
            if exc.code == "55000":
                # Refused without effect: the epoch rotated after it was read,
                # or the name is already held (possibly by this account).
                current = _epoch(rpc)
                if current != epoch:
                    epoch = current
                    continue
                if _own_username(rpc) == username:
                    return ALREADY_MEMBER
                raise MemberClaimError(
                    "taken", f"@{username} is already claimed on {rpc.root} "
                    "by someone else") from None
            if exc.code not in (None, "40001"):
                raise MemberClaimError(
                    "refused", f"the provider refused the claim ({exc.code})") from None
            # Unknown outcome or a contended slot: replay the exact operation.
            sleep(_retry_delay(attempt))
            continue
        _acknowledge(rpc, receipt, attempts=attempts, sleep=sleep)
        return CLAIMED
    raise MemberClaimError("unknown", "the claim's outcome is unknown")


def _acknowledge(rpc, receipt, *, attempts: int, sleep) -> None:
    for attempt in range(attempts):
        try:
            rpc.acknowledge(receipt)
            return
        except TypedMutationRPCError as exc:
            if exc.code not in (None, "40001"):
                raise MemberClaimError(
                    "refused", f"the provider refused the claim's acknowledgment "
                    f"({exc.code})") from None
            sleep(_retry_delay(attempt))
    raise MemberClaimError(
        "unknown", "the name is claimed, but its receipt is not yet acknowledged")
