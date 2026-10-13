"""Real PostgreSQL session schedules for prospective mutation admission.

Called by the disposable protocol validator after installing its schema. No
network or database work occurs on import; all gates use that explicit database.
"""
from __future__ import annotations


def _validate_provider_admission_races(database_url: str) -> None:
    """Admission, cancellation, effects and closure share durable lock order."""
    import json
    import time
    from uuid import UUID, uuid4

    # Keep imports here: the validator calls this companion. In direct-script
    # mode use its existing module, including the GateFailure class it catches.
    try:
        from __main__ import (
            GateFailure, _PsqlSession, _claims, _jsonb, _prepare_root, _quote,
            _receipt_sql, _run, _scalar, _seal_sql,
        )
    except ImportError:
        from devtools.validate_mutation_protocol_postgres import (
            GateFailure, _PsqlSession, _claims, _jsonb, _prepare_root, _quote,
            _receipt_sql, _run, _scalar, _seal_sql,
        )

    actor = uuid4()

    def authenticated(sql):
        return 'begin; set local role authenticated; ' + _claims(actor) + sql + '; commit;'

    def result(sql):
        return json.loads(_scalar(database_url, authenticated(sql)))

    def setup(suffix):
        root = 'admission-race-' + suffix
        initial = _prepare_root(database_url, root, actor)
        sealed = json.loads(_scalar(database_url, _seal_sql(root, initial, uuid4())))
        epoch = UUID(sealed['protocol_epoch'])
        _run(database_url, 'select public.ab_admin_enable_mutation_admission('
             f'{_quote(root)}, {_quote(str(epoch))}::uuid);')
        return root, epoch, uuid4(), {
            'operation_id': str(uuid4()), 'kind': 'doc_put', 'client_digest': 'a' * 64,
        }

    def admit(root, epoch, owner, leg):
        return ('select public.ab_admit_mutation_owner('
                f'{_quote(root)}, {_quote(str(epoch))}::uuid, '
                f'{_quote(str(owner))}::uuid, {_jsonb([leg])})')

    def cancel(root, epoch, owner, leg, *, ack=False):
        name = 'ab_ack_mutation_admission_cancel' if ack else 'ab_cancel_mutation_admission'
        return (f'select public.{name}({_quote(root)}, {_quote(str(epoch))}::uuid, '
                f'{_quote(str(owner))}::uuid, {_quote(leg["operation_id"])}::uuid, '
                f'{_quote(leg["kind"])}, {_quote(leg["client_digest"])})')

    def put(root, epoch, leg):
        return ('select public.ab_mutation_doc_put('
                f'{_quote(root)}, {_quote(str(epoch))}::uuid, '
                f'{_quote(leg["operation_id"])}::uuid, {_quote(leg["client_digest"])}, '
                "'race.json', '{\"value\":1}'::jsonb)")

    def rotate(root, epoch):
        return ('select public.ab_admin_rotate_mutation_epoch('
                f'{_quote(root)}, {_quote(str(epoch))}::uuid, {_quote(str(uuid4()))}::uuid)')

    def current_epoch(root):
        return _scalar(database_url, 'select protocol_epoch::text from '
                       f'public.ab_mutation_roots where root={_quote(root)};')

    def source_count(root):
        return _scalar(database_url, 'select count(*) from public.ab_docs '
                       f'where root={_quote(root)} and path=\'race.json\';')

    def receipt_count(root):
        return _scalar(database_url, 'select count(*) from public.ab_mutation_receipts '
                       f'where root={_quote(root)};')

    def leave(root, epoch):
        # Exercise real sealed-root membership removal, not a fixture DELETE
        # which correctly fails the legacy-write trigger after seal.
        owner = uuid4()
        leg = {'operation_id': str(uuid4()), 'kind': 'member_leave',
               'client_digest': 'b' * 64}
        result(admit(root, epoch, owner, leg))
        receipt = result('select public.ab_mutation_member_leave('
                         f'{_quote(root)}, {_quote(str(epoch))}::uuid, '
                         f'{_quote(leg["operation_id"])}::uuid, '
                         f'{_quote(leg["client_digest"])})')
        _run(database_url, authenticated(_receipt_sql(
            'ab_ack_mutation_receipt', root, epoch, UUID(leg['operation_id']),
            receipt['client_digest'], receipt['provider_digest'])))

    def contend(holder_sql, waiter_sql, *, holder_admin=False,
                waiter_admin=False, denied=None):
        suffix = uuid4().hex
        holder_name, waiter_name = 'admit-holder-' + suffix, 'admit-waiter-' + suffix
        ready, committed, done = 'ready-' + suffix, 'committed-' + suffix, 'done-' + suffix
        holder, waiter = _PsqlSession(database_url), _PsqlSession(database_url)
        try:
            holder.send(f'set application_name={_quote(holder_name)}; '
                        "set statement_timeout='12s'; begin; "
                        + ('' if holder_admin else 'set local role authenticated; ' + _claims(actor))
                        + holder_sql + f'; select {_quote(ready)};')
            holder.wait_marker(ready)
            waiter.send(f'set application_name={_quote(waiter_name)}; '
                        "set statement_timeout='12s'; begin; "
                        + ('' if waiter_admin else 'set local role authenticated; ' + _claims(actor))
                        + waiter_sql + f'; commit; select {_quote(done)};')
            # An observed blocker PID, not elapsed time, establishes the race.
            # Each bounded psql query yields to both participants naturally.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if holder.process.poll() is not None:
                    raise GateFailure(holder._failure('admission holder exited before barrier'))
                if waiter.process.poll() is not None:
                    raise GateFailure(waiter._failure('admission waiter exited before barrier'))
                blocked = _scalar(database_url,
                    'select exists(select 1 from pg_catalog.pg_stat_activity h '
                    'join pg_catalog.pg_stat_activity w on '
                    'h.pid = any(pg_catalog.pg_blocking_pids(w.pid)) '
                    f'where h.application_name={_quote(holder_name)} '
                    f'and w.application_name={_quote(waiter_name)});')
                if blocked == 't':
                    break
            else:
                raise GateFailure('admission participants did not reach the held-lock barrier')
            holder.send(f'commit; select {_quote(committed)};')
            holder.wait_marker(committed)
            holder.finish()
            if denied is None:
                waiter.wait_marker(done)
                waiter.finish()
            else:
                error = waiter.finish(expect_ok=False)
                if denied not in error.lower():
                    raise GateFailure(f'admission waiter failed for the wrong reason: {error.strip()}')
        finally:
            holder.abort()
            waiter.abort()

    def legs(root):
        return _scalar(database_url, 'select count(*) from public.ab_mutation_admission_legs '
                       f'where root={_quote(root)};')

    # Option B: an in-flight unadmitted write holds the admission mutex shared,
    # so an admission of the same operation waits, then sees its receipt.
    root, epoch, owner, leg = setup('unadmitted-first')
    contend(put(root, epoch, leg), admit(root, epoch, owner, leg), denied='collision')
    if source_count(root) != '1' or receipt_count(root) != '1' or legs(root) != '0':
        raise GateFailure('admission reserved an operation that executed unadmitted')

    # Conversely an execution waiting on a committing admission uses its leg.
    root, epoch, owner, leg = setup('admitted-first')
    contend(admit(root, epoch, owner, leg), put(root, epoch, leg))
    if _scalar(database_url, 'select bool_and(r.receipt_slot = l.receipt_slot) '
               'from public.ab_mutation_receipts r join public.ab_mutation_admission_legs l '
               'using (root, protocol_epoch, operation_id) '
               f'where r.root={_quote(root)};') != 't' or receipt_count(root) != '1':
        raise GateFailure('execution after admission ignored its reserved leg')

    # Unadmitted writers share the mutex: one in flight never blocks another.
    root, epoch, _owner, first = setup('unadmitted-shared')
    second = {'operation_id': str(uuid4()), 'kind': 'doc_put', 'client_digest': 'c' * 64}
    marker = 'shared-' + uuid4().hex
    holder = _PsqlSession(database_url)
    try:
        holder.send("set statement_timeout='12s'; begin; set local role authenticated; "
                    + _claims(actor) + put(root, epoch, first) + f'; select {_quote(marker)};')
        holder.wait_marker(marker)
        _scalar(database_url, "begin; set local statement_timeout='3s'; "
                "set local role authenticated; " + _claims(actor)
                + 'select public.ab_mutation_doc_put('
                f'{_quote(root)}, {_quote(str(epoch))}::uuid, '
                f'{_quote(second["operation_id"])}::uuid, {_quote(second["client_digest"])}, '
                "'shared.json', '{\"value\":2}'::jsonb); commit;")
        holder.send('commit;')
        holder.finish()
    finally:
        holder.abort()
    if receipt_count(root) != '2':
        raise GateFailure('concurrent unadmitted writes did not both commit')

    # A committed effect must win against a cancellation waiting on that leg.
    root, epoch, owner, leg = setup('execute-first')
    result(admit(root, epoch, owner, leg))
    effect, cancellation = put(root, epoch, leg), cancel(root, epoch, owner, leg)
    contend(effect, cancellation)
    receipt = result(effect)
    executed = result(cancellation)
    if executed['status'] != 'executed' or source_count(root) != '1' or receipt_count(root) != '1':
        raise GateFailure('execute-before-cancel lost its sole effect or fabricated cancellation')
    # Outcome recovery belongs to the original actor even after membership loss.
    leave(root, epoch)
    if result(effect) != receipt or result(cancellation) != executed:
        raise GateFailure('removed actor could not replay committed admission outcome')
    if _scalar(database_url, authenticated(_receipt_sql(
            'ab_ack_mutation_receipt', root, epoch, UUID(leg['operation_id']),
            receipt['client_digest'], receipt['provider_digest']))) != 't':
        raise GateFailure('removed actor could not acknowledge the exact effect')
    _run(database_url, rotate(root, epoch) + ';')

    # Cancellation must win if its transaction precedes dispatch on the same leg.
    root, epoch, owner, leg = setup('cancel-first')
    result(admit(root, epoch, owner, leg))
    cancellation = cancel(root, epoch, owner, leg)
    contend(cancellation, put(root, epoch, leg), denied='cancel')
    cancelled = result(cancellation)
    if cancelled['status'] != 'cancelled' or source_count(root) != '0' or receipt_count(root) != '0':
        raise GateFailure('cancel-before-execute produced an effect or mutation receipt')
    leave(root, epoch)
    if result(cancellation) != cancelled:
        raise GateFailure('removed actor lost exact cancellation replay')
    # Rotation cannot cross an unacknowledged cancelled leg. Once the ACK
    # commits, the waiting rotation can close the epoch without any effect.
    unacked = _run(database_url, rotate(root, epoch) + ';', expect_ok=False)
    if 'acknowledged' not in unacked.stderr.lower():
        raise GateFailure('unacknowledged cancellation failed rotation for the wrong reason')
    contend(cancel(root, epoch, owner, leg, ack=True), rotate(root, epoch), waiter_admin=True)
    if current_epoch(root) == str(epoch) or source_count(root) != '0':
        raise GateFailure('cancellation ACK did not safely unblock epoch rotation')

    # New admission pins the epoch before a contending admin rotation can close.
    root, epoch, owner, leg = setup('admit-first')
    admission = admit(root, epoch, owner, leg)
    contend(admission, rotate(root, epoch), waiter_admin=True, denied='acknowledged')
    if current_epoch(root) != str(epoch) or result(admission)['status'] != 'admitted':
        raise GateFailure('rotation crossed a durable admitted owner')
    if source_count(root) != '0':
        raise GateFailure('effect-free admission changed a source')
    result(cancel(root, epoch, owner, leg))
    _run(database_url, authenticated(cancel(root, epoch, owner, leg, ack=True)))
    _run(database_url, rotate(root, epoch) + ';')

    # Conversely, admission of an old epoch must fail once rotation commits.
    root, epoch, owner, leg = setup('rotate-first')
    contend(rotate(root, epoch), admit(root, epoch, owner, leg),
            holder_admin=True, denied='epoch')
    if current_epoch(root) == str(epoch) or source_count(root) != '0' or receipt_count(root) != '0':
        raise GateFailure('stale admission resurrected an old epoch or source effect')
    stale = _run(database_url, authenticated(admit(root, epoch, owner, leg)), expect_ok=False)
    if 'epoch' not in stale.stderr.lower():
        raise GateFailure('stale admission replay failed for the wrong reason')
