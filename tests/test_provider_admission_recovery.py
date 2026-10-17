"""Independent admission capacity and concurrent replay regressions."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
import test_operation_journal as base
import test_provider_admission_owner as cases
from agentbridge.store import local_source, operation_journal as oj
from agentbridge.store.provider_admission_owner import (
    reservation_capacity,
    ProviderAdmissionOwnerJournal,
)

world = base.world


def test_exact_capacity_transfers_to_two_tickets_and_guards_release(world, monkeypatch):
    owner, client = cases._owner(world)
    requests = tuple(
        base._request(i, arguments={"p_path": f"presence/{i}.json", "p_data": {"value": i}})
        for i in (51, 52)
    )
    prepared = owner.prepare(cases.OWNER, requests)
    with sqlite3.connect(world[0].path) as conn:
        future_count, future_size = reservation_capacity(conn)
    assert future_count == 2
    monkeypatch.setattr(oj, "MAX_RECORDS", 2)
    monkeypatch.setattr(oj, "MAX_BYTES", future_size + 2 * oj.MAX_RECEIPT_BYTES)
    client.on_execute = lambda *a: cases._reply(prepared.plan, "admitted")
    assert owner.admit(cases.OWNER).status == "admitted"
    tickets = [owner.bind_ticket(cases.OWNER, r.expectation.operation_id) for r in requests]
    with sqlite3.connect(world[0].path) as conn:
        assert reservation_capacity(conn)[0] == 0
        assert conn.execute("SELECT count(*) FROM typed_operations").fetchone() == (2,)
        assert conn.execute("SELECT count(*) FROM mutation_intents").fetchone() == (2,)
    with pytest.raises(local_source.SourceChanged, match="record_budget"):
        owner.journal.allocate_ticket("doc_put", {"p_path": "presence/extra.json", "p_data": {}})
    restarted = ProviderAdmissionOwnerJournal(base._reload(world), cases._wire()[0])
    assert [
        restarted.bind_ticket(cases.OWNER, r.expectation.operation_id) for r in requests
    ] == tickets
    for request, ticket in zip(requests, tickets):
        assert restarted.journal.get_ticket(ticket).request == request
        restarted.journal.mark_attempted(request)
        restarted.journal.accept(request, base._receipt(request))
        restarted.journal.complete(request)
        restarted.journal.mark_acknowledged(request)
        with pytest.raises(local_source.SourceChanged, match="admission_owner_unconsumed"):
            restarted.journal.release_ticket(ticket)
        with pytest.raises(local_source.SourceChanged, match="admission_leg"):
            restarted.journal.prepare_operation(request)


def test_two_unknown_admission_replays_link_one_ticket(world):
    owner, client = cases._owner(world)
    request = base._request(71, arguments={"p_path": "presence/race.json", "p_data": {}})
    frozen = owner.prepare(cases.OWNER, (request,))
    barrier = threading.Barrier(2)

    def reply(*a):
        barrier.wait(timeout=5)
        return cases._reply(frozen.plan, "admitted")

    client.on_execute = reply

    def worker():
        assert owner.admit(cases.OWNER).status == "admitted"
        return owner.bind_ticket(cases.OWNER, request.expectation.operation_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker) for _ in range(2)]
        tickets = [future.result(timeout=10) for future in futures]
    assert tickets[0] == tickets[1]
    with sqlite3.connect(world[0].path) as conn:
        assert conn.execute("SELECT count(*) FROM typed_operations").fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM mutation_intents").fetchone() == (1,)
        assert conn.execute(
            "SELECT linked,cancel,outcome FROM typed_admission_legs"
        ).fetchall() == [(1, "none", None)]
