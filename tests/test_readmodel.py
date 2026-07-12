"""Read-model fold rules — the pure-function heart of the choke point."""

from agentbridge.core.models import BodyRecord, Envelope, MsgKind
from agentbridge.mesh.readmodel import build_messages, parse_tags, unread_info
from agentbridge.mesh.sealer import PlainSealer

SEALER = PlainSealer()


def env(msg_id, ns, sender, body="", kind=MsgKind.MESSAGE, **body_kw):
    e = Envelope(id=msg_id, ns=ns, ts="t", from_=sender, kind=kind)
    if kind is MsgKind.MESSAGE:
        sealed = SEALER.seal("c", BodyRecord(body=body, **body_kw))
        e.epoch, e.nonce, e.ct, e.sig = (
            sealed["epoch"], sealed["nonce"], sealed["ct"], sealed["sig"],
        )
    return e.to_dict()


def test_parse_tags():
    assert parse_tags("hey @claude and @CoCo — also @all") == ["claude", "coco", "all"]
    assert parse_tags("mail me a@b.com") == ["b.com"]  # v1 behaviour: plain @word
    assert parse_tags("") == []


def test_dedup_and_ns_order_with_ties():
    envs = [
        env("m2", 10, "bob", "second"),
        env("m1", 5, "ann", "first"),
        env("m2", 10, "bob", "second"),      # duplicate id (at-least-once)
        env("m3", 10, "ann", "tie-breaker"),  # ns tie -> (ns, from) order
    ]
    out = build_messages("c", "ann", envs, SEALER)
    assert [m.id for m in out] == ["m1", "m3", "m2"]


def test_edit_applies_author_only_and_redaction_wins():
    envs = [env("m1", 1, "ann", "original secret")]
    edit = {**SEALER.seal("c", BodyRecord(body="edited", tags=[])),
            "by": "ann", "at": "t2", "ns": 9}
    out = build_messages("c", "bob", envs, SEALER, edits={"m1": edit})
    assert out[0].body == "edited" and out[0].edited["ns"] == 9

    # a forged edit by a non-author is ignored on read
    forged = {**edit, "by": "bob"}
    out2 = build_messages("c", "bob", envs, SEALER, edits={"m1": forged})
    assert out2[0].body == "original secret" and out2[0].edited is None

    # sneaky edit-then-delete: the tombstone must win
    out3 = build_messages(
        "c", "bob", envs, SEALER,
        edits={"m1": edit}, redactions={"m1": {"by": "ann"}},
    )
    m = out3[0]
    assert m.deleted and m.body == "" and m.edited is None and m.tags == []


def test_reply_quote_to_redacted_parent_blanked():
    envs = [
        env("m1", 1, "ann", "parent"),
        env("m2", 2, "bob", "child", reply_to={"id": "m1", "body": "parent", "from": "ann"}),
    ]
    out = build_messages("c", "bob", envs, SEALER, redactions={"m1": {"by": "ann"}})
    child = [m for m in out if m.id == "m2"][0]
    assert child.reply_to == {"id": "m1", "deleted": True}  # no leaked body


def test_hidden_and_cleared_with_keep_starred():
    envs = [env(f"m{i}", i, "ann", f"b{i}") for i in range(1, 5)]
    state = {
        "hidden": ["m4"],
        "cleared": {"ns": 2, "keep_starred": True},
        "starred": ["m1"],
    }
    out = build_messages("c", "me", envs, SEALER, state=state)
    # m1 starred+kept, m2 cleared, m3 past the cut, m4 hidden
    assert [m.id for m in out] == ["m1", "m3"]


def test_reactions_attached():
    envs = [env("m1", 1, "ann", "hi")]
    out = build_messages(
        "c", "bob", envs, SEALER, reactions={"m1": {"👍": ["bob", "coco"]}}
    )
    assert out[0].reactions == {"👍": ["bob", "coco"]}


def test_info_events_pass_through():
    e = Envelope(id="i1", ns=1, ts="t", from_="ann", kind=MsgKind.INFO,
                 event={"type": "member_added", "who": "coco"})
    out = build_messages("c", "bob", [e.to_dict()], SEALER)
    assert out[0].kind is MsgKind.INFO and out[0].event["who"] == "coco"


def test_unread_info_including_edit_marks_unread():
    envs = [
        env("m1", 10, "ann", "old"),
        env("m2", 20, "ann", "new"),
        env("m3", 30, "me", "mine doesn't count"),
    ]
    edit = {**SEALER.seal("c", BodyRecord(body="old v2")), "by": "ann", "at": "t", "ns": 25}
    msgs = build_messages("c", "me", envs, SEALER, edits={"m1": edit})

    info = unread_info(msgs, "me", {"read_ns": 15})
    # m2 is new (20>15); m1 read at 15 but EDITED at 25 -> unread again
    assert info["unread"] == 2 and info["first_unread_ns"] == 10

    info2 = unread_info(msgs, "me", {"read_ns": 30})
    assert info2["unread"] == 0
    info3 = unread_info(msgs, "me", {"read_ns": 30, "forced_unread": True})
    assert info3["forced_unread"] is True
