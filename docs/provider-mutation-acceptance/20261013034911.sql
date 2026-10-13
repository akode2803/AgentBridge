-- AgentBridge Scratch migration 20261013034911
-- Project: riagjoluhcwjvmfchbzl
-- Name: agentbridge_p1b_typed_effect_transition_rpc
-- Applied statement bytes: 8262
-- Applied statement SHA-256: 7ed0e4a1170573705e667c6e2c21be9ebb0f895d39d63a4119eb69b9e3f8bb04
-- BEGIN APPLIED STATEMENT

-- P1b effect slice: the existing R142 transition remains the semantic effect
-- validator/writer.  This inactive wrapper adds sealed-root receipt identity,
-- holds mutable authority through that write and rejects a fresh operation for
-- an already-existing effect path.  Exact response-loss recovery uses only the
-- operation receipt above, rather than R142's legacy same-document retry.
create or replace function public.ab_mutation_effect_transition(
  p_root text, p_protocol_epoch uuid, p_operation_id uuid,
  p_payload_digest text, p_path text, p_data jsonb,
  p_grant_ask jsonb, p_grant_decision jsonb
) returns jsonb
language plpgsql volatile security definer set search_path = pg_catalog as $$
declare
  v_actor uuid := auth.uid();
  v_member text;
  v_chat text;
  v_run text;
  v_call text;
  v_file text;
  v_effect_actor text;
  v_meta jsonb;
  v_actor_doc jsonb;
  v_room jsonb;
  v_previous text;
  v_previous_data jsonb;
  v_existing jsonb;
  v_provider_digest text;
  v_begin jsonb;
  v_transitioned boolean;
begin
  if not private.ab_mutation_path_valid(p_path, 4096, 32)
     or p_path !~ '^chats/[^/]+/runtime/effects/[^/]+/[^/]+/(claim|state-[23])\.json$'
     or p_data is null
     or pg_catalog.jsonb_typeof(p_data) <> 'object'
     or public.ab_source_json_bytes(p_data) > 8355840
     or (
       p_grant_ask is not null
       and public.ab_source_json_bytes(p_grant_ask) > 8355840
     )
     or (
       p_grant_decision is not null
       and public.ab_source_json_bytes(p_grant_decision) > 8355840
     ) then
    raise exception 'invalid typed effect transition' using errcode = '22023';
  end if;
  v_chat := pg_catalog.split_part(p_path, '/', 2);
  v_run := pg_catalog.split_part(p_path, '/', 5);
  v_call := pg_catalog.split_part(p_path, '/', 6);
  v_file := pg_catalog.split_part(p_path, '/', 7);
  v_meta := p_data->'meta';
  v_effect_actor := v_meta->>'actor';
  if pg_catalog.jsonb_typeof(v_meta) is distinct from 'object'
     or v_effect_actor is null or v_effect_actor = ''
     or v_meta->>'kind' is distinct from 'effect'
     or v_meta->>'signer' is distinct from v_effect_actor
     or v_meta->>'chat_id' is distinct from v_chat
     or v_meta->>'run_id' is distinct from v_run
     or v_meta->>'root_run_id' is distinct from v_run
     or v_meta->>'call_id' is distinct from v_call then
    raise exception 'invalid typed effect transition' using errcode = '22023';
  end if;
  if v_file = 'claim.json' then
    if pg_catalog.jsonb_typeof(p_grant_ask) is distinct from 'object'
       or pg_catalog.jsonb_typeof(p_grant_decision) is distinct from 'object'
       or p_grant_ask->'header'->>'kind' is distinct from 'permission_ask'
       or p_grant_ask->'header'->>'sender' is distinct from v_effect_actor
       or p_grant_ask->'header'->>'agent' is distinct from v_effect_actor
       or p_grant_ask->'header'->>'chat_id' is distinct from v_chat
       or p_grant_decision->'header'->>'kind'
            is distinct from 'permission_decision'
       or p_grant_decision->'header'->>'recipient'
            is distinct from v_effect_actor
       or p_grant_decision->'header'->>'agent'
            is distinct from v_effect_actor
       or p_grant_decision->'header'->>'chat_id' is distinct from v_chat then
      raise exception 'invalid typed effect transition' using errcode = '22023';
    end if;
  elsif p_grant_ask is not null or p_grant_decision is not null then
    raise exception 'invalid typed effect transition' using errcode = '22023';
  end if;
  v_provider_digest := private.ab_mutation_provider_digest(
    pg_catalog.jsonb_build_array(
      'effect_transition', p_path, p_data, p_grant_ask, p_grant_decision
    )
  );
  if not private.ab_mutation_root_sealed(p_root) then
    raise exception 'typed mutation root is not sealed' using errcode = '55000';
  end if;
  v_begin := private.ab_mutation_begin(
    p_root, p_protocol_epoch, p_operation_id, 'effect_transition',
    p_payload_digest, v_provider_digest
  );
  if (v_begin->>'replay')::boolean then
    return v_begin->'receipt';
  end if;

  v_member := private.ab_mutation_lock_actor(p_root);
  v_chat := pg_catalog.split_part(p_path, '/', 2);
  if v_actor is null or v_member = '' then
    raise exception 'typed effect transition is unauthorized'
      using errcode = '42501';
  end if;
  perform private.ab_mutation_lock_chat(p_root, v_chat, false);
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      'agentbridge-effect-transition:'
      || pg_catalog.jsonb_build_array(p_root, p_path)::text, 0
    )
  );

  -- Agent ownership/active state is part of R142 authority and may change
  -- independently of room metadata.  Hold the exact actor document stable
  -- while the existing transition revalidates and writes.  Room metadata and
  -- cleanup are stable under the shared room lock above.
  select d.data into v_actor_doc
  from public.ab_docs d
  where d.root = p_root
    and d.path = 'users/' || v_effect_actor || '.json'
    and not d.deleted
  for share;

  select d.data into v_room
  from public.ab_docs d
  where d.root = p_root
    and d.path = 'chats/' || v_chat || '/meta.json'
    and not d.deleted
  for share;
  if pg_catalog.jsonb_typeof(v_actor_doc) is distinct from 'object'
     or not coalesce(
       v_actor_doc @> '{"kind":"agent","active":true}'::jsonb, false
     )
     or pg_catalog.jsonb_typeof(v_actor_doc->'agent')
          is distinct from 'object'
     or v_actor_doc->'agent'->>'owner' is distinct from v_member
     or pg_catalog.jsonb_typeof(v_room) is distinct from 'object'
     or not coalesce(v_room->'members' ? v_member, false)
     or not coalesce(v_room->'members' ? v_effect_actor, false) then
    raise exception 'typed effect transition is unauthorized'
      using errcode = '42501';
  end if;
  if v_file = 'claim.json' then
    if p_grant_ask->'header'->>'recipient' is distinct from v_member
       or p_grant_decision->'header'->>'sender' is distinct from v_member then
      raise exception 'typed effect transition is unauthorized'
        using errcode = '42501';
    end if;
  elsif v_file = 'state-2.json' then
    v_previous := 'chats/' || v_chat || '/runtime/effects/'
      || v_run || '/' || v_call || '/claim.json';
  else
    v_previous := 'chats/' || v_chat || '/runtime/effects/'
      || v_run || '/' || v_call || '/state-2.json';
  end if;
  if v_previous is not null then
    select d.data into v_previous_data
    from public.ab_docs d
    where d.root = p_root and d.path = v_previous and not d.deleted;
    if v_previous_data is null
       or v_previous_data->'meta'->>'actor' is distinct from v_effect_actor
       or v_previous_data->'meta'->>'chat_id' is distinct from v_chat
       or v_previous_data->'meta'->>'run_id' is distinct from v_run
       or v_previous_data->'meta'->>'call_id' is distinct from v_call then
      raise exception 'typed effect transition predecessor is unavailable'
        using errcode = '55000';
    end if;
  end if;

  perform 1 from public.ab_docs d
  where d.root = p_root and d.path = p_path;
  if found then
    raise exception 'typed effect transition already exists'
      using errcode = '55000';
  end if;

  v_transitioned := public.ab_effect_transition(
    p_root, p_path, p_data, p_grant_ask, p_grant_decision
  );
  if v_transitioned is distinct from true then
    raise exception 'typed effect transition is unauthorized'
      using errcode = '42501';
  end if;
  select d.data into v_existing
  from public.ab_docs d
  where d.root = p_root and d.path = p_path and not d.deleted;
  if not found or v_existing is distinct from p_data then
    raise exception 'typed effect transition did not commit exact effect'
      using errcode = '55000';
  end if;

  return private.ab_mutation_finish(
    p_root, p_protocol_epoch, p_operation_id, 'effect_transition',
    p_payload_digest, v_provider_digest,
    pg_catalog.jsonb_build_object('path', p_path, 'transitioned', true)
  );
end
$$;
revoke all on function public.ab_mutation_effect_transition(
  text,uuid,uuid,text,text,jsonb,jsonb,jsonb)
  from public, anon, authenticated, service_role;
grant execute on function public.ab_mutation_effect_transition(
  text,uuid,uuid,text,text,jsonb,jsonb,jsonb) to authenticated;
