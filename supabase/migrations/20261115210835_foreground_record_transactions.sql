-- Interactive writes use short compare-and-set transactions rather than the
-- long ingestion lease. Existing workers are fenced if a newer user commit
-- makes their protected input snapshot stale during a rolling deployment.
alter table jobflow_private.workspaces
  add column if not exists foreground_revision bigint not null default 0,
  add column if not exists lease_foreground_revision bigint not null default 0;

create or replace function public.jobflow_workspace_lease(
  p_workspace uuid,p_token text,p_seconds integer,p_action text)
returns boolean language plpgsql security invoker set search_path='' as $$
declare affected integer;
begin
  if p_token is null or p_token !~ '^[A-Za-z0-9_-]{32,128}$'
     or p_seconds not between 30 and 3600 then
    raise exception 'Invalid lease request' using errcode='22023';
  end if;
  if p_action='acquire' then
    update jobflow_private.workspaces set lease_token=p_token,
      lease_until=clock_timestamp()+make_interval(secs=>p_seconds),
      lease_foreground_revision=foreground_revision
      where id=p_workspace and (lease_until is null or lease_until<=clock_timestamp());
  elsif p_action='renew' then
    update jobflow_private.workspaces set lease_until=clock_timestamp()+make_interval(secs=>p_seconds)
      where id=p_workspace and lease_token=p_token and lease_until>clock_timestamp();
  elsif p_action='release' then
    update jobflow_private.workspaces set lease_token=null,lease_until=null
      where id=p_workspace and lease_token=p_token;
  else
    raise exception 'Invalid lease action' using errcode='22023';
  end if;
  get diagnostics affected=row_count;
  return affected=1;
end $$;

create or replace function public.jobflow_records_put(
  p_workspace uuid,p_token text,p_rows jsonb,p_import boolean default false)
returns integer language plpgsql security invoker set search_path='' as $$
declare affected integer;
begin
  perform jobflow_private.assert_lease(p_workspace,p_token);
  if jsonb_typeof(p_rows) is distinct from 'array' or jsonb_array_length(p_rows)>10000 then
    raise exception 'Invalid record batch' using errcode='22023';
  end if;
  if exists(select 1 from jsonb_to_recordset(p_rows)
      as x(kind text,id text,body jsonb,created_at text,updated_at text)
      where x.kind is null or x.kind !~ '^[a-z][a-z0-9_]{0,63}$'
       or x.id is null or length(x.id) not between 1 and 512
       or jsonb_typeof(x.body) is distinct from 'object'
       or x.created_at is null or x.updated_at is null) then
    raise exception 'Invalid record' using errcode='22023';
  end if;
  if exists(select 1 from jsonb_array_elements(p_rows) x
      where x->>'kind' not in ('jobs','source_health','schedule'))
     and exists(select 1 from jobflow_private.workspaces
      where id=p_workspace and foreground_revision<>lease_foreground_revision) then
    raise exception 'Foreground records changed; retry with current inputs' using errcode='40001';
  end if;
  if p_import and exists(select 1 from jsonb_to_recordset(p_rows)
      as x(kind text,id text,body jsonb,created_at text,updated_at text)
      join jobflow_private.records r on r.workspace_id=p_workspace and r.kind=x.kind and r.id=x.id
      where (r.body,r.created_at,r.updated_at) is distinct from (x.body,x.created_at,x.updated_at)) then
    raise exception 'Import conflicts with existing records' using errcode='22023';
  end if;
  insert into jobflow_private.records(workspace_id,kind,id,body,created_at,updated_at)
    select p_workspace,x.kind,x.id,x.body,x.created_at,x.updated_at
    from jsonb_to_recordset(p_rows) as x(kind text,id text,body jsonb,created_at text,updated_at text)
    on conflict(workspace_id,kind,id) do update set body=excluded.body,updated_at=excluded.updated_at;
  get diagnostics affected=row_count;
  if affected>0 then
    update jobflow_private.workspaces set revision=revision+1 where id=p_workspace;
  end if;
  return affected;
end $$;

create or replace function public.jobflow_records_compare_and_set(
  p_workspace uuid,p_rows jsonb,p_expected jsonb,p_collections jsonb,
  p_foreground boolean default false,p_token text default null,
  p_files jsonb default '[]'::jsonb,p_refresh boolean default false)
returns jsonb language plpgsql security invoker set search_path='' as $$
declare collection jsonb; current_ids jsonb; account_id uuid; result_revision bigint;
begin
  if jsonb_typeof(p_rows) is distinct from 'array' or jsonb_array_length(p_rows)>10000
    or jsonb_typeof(p_expected) is distinct from 'array' or jsonb_array_length(p_expected)>10000
    or jsonb_typeof(p_collections) is distinct from 'array' or jsonb_array_length(p_collections)>100
    or jsonb_typeof(p_files) is distinct from 'array' or jsonb_array_length(p_files)>100
    or p_foreground is null or p_refresh is null then
    raise exception 'Invalid record transaction' using errcode='22023';
  end if;
  if exists(select 1 from jsonb_to_recordset(p_rows)
      as x(kind text,id text,body jsonb,created_at text,updated_at text)
      where x.kind is null or x.kind !~ '^[a-z][a-z0-9_]{0,63}$'
       or x.id is null or length(x.id) not between 1 and 512
       or jsonb_typeof(x.body) is distinct from 'object'
       or x.created_at is null or x.updated_at is null) then
    raise exception 'Invalid record' using errcode='22023';
  end if;
  if exists(select 1 from jsonb_to_recordset(p_expected) as x(kind text,id text,body jsonb)
      where x.kind is null or x.id is null
       or (x.body is not null and jsonb_typeof(x.body) not in ('object','null')))
    or exists(select 1 from jsonb_array_elements(p_expected) x where not (x ? 'body')) then
    raise exception 'Invalid expected record' using errcode='22023';
  end if;
  if p_foreground and exists(select 1 from jsonb_array_elements(p_rows) x
      where x->>'kind' not in ('applications','interviews','tasks','activities','plans','coaching',
        'application_batches','application_resumes','resume_profile','resume_drafts','resume_reviews')) then
    raise exception 'This record requires a background workflow' using errcode='22023';
  end if;
  if not p_foreground and (jsonb_array_length(p_files)>0 or p_refresh) then
    raise exception 'Invalid background transaction options' using errcode='22023';
  end if;
  -- Only the duration of this DB transaction is serialized, never feed fetches,
  -- report rendering, uploads, subprocess execution, or browser editing time.
  perform 1 from jobflow_private.workspaces where id=p_workspace for update;
  if not found then raise exception 'Unknown workspace' using errcode='22023'; end if;
  if not p_foreground then perform jobflow_private.assert_lease(p_workspace,p_token); end if;
  if exists(select 1 from jsonb_to_recordset(p_expected) as x(kind text,id text,body jsonb)
      left join jobflow_private.records r on r.workspace_id=p_workspace and r.kind=x.kind and r.id=x.id
      where r.body is distinct from nullif(x.body,'null'::jsonb)) then
    return jsonb_build_object('ok',false,'conflict',true);
  end if;
  -- Every changed shared row must have an explicit previous-value expectation.
  if exists(select 1 from jsonb_array_elements(p_rows) x
      where x->>'kind' not in ('jobs','source_health','schedule') and not exists(
        select 1 from jsonb_array_elements(p_expected) e
        where e->>'kind'=x->>'kind' and e->>'id'=x->>'id')) then
    raise exception 'Missing record expectation' using errcode='22023';
  end if;
  for collection in select value from jsonb_array_elements(p_collections) loop
    if collection->>'kind' is null or jsonb_typeof(collection->'ids') is distinct from 'array' then
      raise exception 'Invalid collection expectation' using errcode='22023';
    end if;
    select coalesce(jsonb_agg(r.id order by r.id),'[]'::jsonb) into current_ids
      from jobflow_private.records r where r.workspace_id=p_workspace and r.kind=collection->>'kind';
    if current_ids is distinct from (select coalesce(jsonb_agg(id order by id),'[]'::jsonb)
      from jsonb_array_elements_text(collection->'ids') as ids(id)) then
      return jsonb_build_object('ok',false,'conflict',true);
    end if;
  end loop;
  if jsonb_array_length(p_files)>0 then
    perform jobflow_private.publish_foreground_files(p_workspace,p_files);
  end if;
  insert into jobflow_private.records(workspace_id,kind,id,body,created_at,updated_at)
    select p_workspace,x.kind,x.id,x.body,x.created_at,x.updated_at
    from jsonb_to_recordset(p_rows) as x(kind text,id text,body jsonb,created_at text,updated_at text)
    on conflict(workspace_id,kind,id) do update set body=excluded.body,updated_at=excluded.updated_at;
  update jobflow_private.workspaces set revision=revision+1,
    foreground_revision=foreground_revision+case when p_foreground then 1 else 0 end
    where id=p_workspace returning revision into result_revision;
  if p_refresh then
    select a.id into account_id from jobflow_private.accounts a where a.workspace_id=p_workspace
      and a.status in ('active','invited') and not a.migration_pending;
    if account_id is not null then perform public.jobflow_queue_enqueue(account_id,'refresh'); end if;
  end if;
  return jsonb_build_object('ok',true,'revision',result_revision);
end $$;

revoke all on function public.jobflow_records_compare_and_set(uuid,jsonb,jsonb,jsonb,boolean,text,jsonb,boolean)
  from public,anon,authenticated;
grant execute on function public.jobflow_records_compare_and_set(uuid,jsonb,jsonb,jsonb,boolean,text,jsonb,boolean)
  to service_role;
