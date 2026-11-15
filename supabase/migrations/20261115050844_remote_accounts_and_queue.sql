-- Server-only authentication state and job queue. No browser role can call these RPCs.
create schema if not exists jobflow_private;
revoke all on schema jobflow_private from public, anon, authenticated;
grant usage on schema jobflow_private to service_role;

create table if not exists jobflow_private.accounts (
  id uuid primary key default gen_random_uuid(),
  email text not null unique check (email = lower(btrim(email)) and length(email) between 3 and 254),
  workspace_id uuid not null unique default gen_random_uuid(),
  identity_id text unique,
  status text not null default 'invited' check (status in ('invited','active','disabled')),
  created_at timestamptz not null default now(),
  onboarding_complete boolean not null default false,
  adopted_at timestamptz,
  migration_pending boolean not null default false,
  migration_source_id text check (migration_source_id ~ '^[0-9a-f]{64}$')
);
create table if not exists jobflow_private.sessions (
  token_hash text primary key check (token_hash ~ '^[0-9a-f]{64}$'),
  account_id uuid not null references jobflow_private.accounts(id) on delete cascade,
  csrf_token text not null check (csrf_token ~ '^[A-Za-z0-9_-]{43}$'),
  created_at double precision not null,
  expires_at double precision not null
);
create index if not exists sessions_account on jobflow_private.sessions(account_id);
create index if not exists sessions_expiry on jobflow_private.sessions(expires_at);
create table if not exists jobflow_private.pending_signins (
  token_hash text primary key check (token_hash ~ '^[0-9a-f]{64}$'),
  email text not null,
  verifier text not null check (verifier ~ '^[A-Za-z0-9_-]{86}$'),
  created_at double precision not null,
  expires_at double precision not null
);
create index if not exists pending_expiry on jobflow_private.pending_signins(expires_at);
create table if not exists jobflow_private.rate_limits (
  key_hash text primary key check (key_hash ~ '^[0-9a-f]{64}$'),
  counter integer not null,
  started_at double precision not null,
  expires_at double precision not null
);
create index if not exists rate_expiry on jobflow_private.rate_limits(expires_at);
create table if not exists jobflow_private.work (
  account_id uuid primary key references jobflow_private.accounts(id) on delete cascade,
  status text not null check (status in ('pending','running','done','partial','failed')),
  command text not null check (command in ('due','refresh','rematch','run')),
  requested_at double precision not null,
  started_at double precision,
  finished_at double precision,
  attempts integer not null default 0,
  available_at double precision not null,
  lease_until double precision,
  claim_token text,
  next_command text check (next_command in ('due','refresh','rematch','run')),
  message text not null default ''
);
create index if not exists work_pending on jobflow_private.work(available_at,requested_at) where status='pending';
create index if not exists work_running on jobflow_private.work(lease_until) where status='running';

alter table jobflow_private.accounts enable row level security;
alter table jobflow_private.sessions enable row level security;
alter table jobflow_private.pending_signins enable row level security;
alter table jobflow_private.rate_limits enable row level security;
alter table jobflow_private.work enable row level security;
revoke all on jobflow_private.accounts, jobflow_private.sessions, jobflow_private.pending_signins,
  jobflow_private.rate_limits, jobflow_private.work from public, anon, authenticated;
grant select,insert,update on jobflow_private.accounts to service_role;
grant select,insert,update,delete on jobflow_private.sessions, jobflow_private.pending_signins,
  jobflow_private.rate_limits, jobflow_private.work to service_role;

create or replace function jobflow_private.account_json(a jobflow_private.accounts)
returns jsonb language sql stable security invoker set search_path='' as $$
 select case when (a).id is null then null else jsonb_build_object(
  'id',(a).id,'email',(a).email,'workspace_id',(a).workspace_id,'status',(a).status,
  'created_at',(a).created_at,'onboarding_complete',(a).onboarding_complete,'adopted_at',(a).adopted_at) end;
$$;
create or replace function jobflow_private.command_priority(command text)
returns integer language sql immutable security invoker set search_path='' as $$
 select case command when 'run' then 3 when 'rematch' then 2 when 'refresh' then 1 when 'due' then 0 else -1 end;
$$;
revoke all on function jobflow_private.account_json(jobflow_private.accounts), jobflow_private.command_priority(text) from public,anon,authenticated;
grant execute on function jobflow_private.account_json(jobflow_private.accounts), jobflow_private.command_priority(text) to service_role;

create or replace function public.jobflow_account_invite(p_email text)
returns jsonb language plpgsql security invoker set search_path='' as $$
declare a jobflow_private.accounts;
begin
 if p_email is null or p_email <> lower(btrim(p_email)) or length(p_email) not between 3 and 254 or p_email !~ '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$' then
  raise exception 'Invalid invitation email';
 end if;
 insert into jobflow_private.accounts(email) values(p_email) on conflict(email) do nothing;
 select * into a from jobflow_private.accounts where email=p_email;
 return jobflow_private.account_json(a);
end $$;
create or replace function public.jobflow_account_lookup(p_email text)
returns jsonb language sql stable security invoker set search_path='' as $$
 select jobflow_private.account_json(a) from jobflow_private.accounts a where email=p_email;
$$;
create or replace function public.jobflow_account_get(p_account uuid)
returns jsonb language sql stable security invoker set search_path='' as $$
 select jobflow_private.account_json(a) from jobflow_private.accounts a where id=p_account;
$$;
create or replace function public.jobflow_account_list()
returns jsonb language sql stable security invoker set search_path='' as $$
 select coalesce(jsonb_agg(jobflow_private.account_json(a) order by created_at,id),'[]'::jsonb) from jobflow_private.accounts a;
$$;
create or replace function public.jobflow_account_authenticate(p_identity text,p_email text)
returns jsonb language plpgsql security invoker set search_path='' as $$
declare a jobflow_private.accounts;
begin
 if p_identity is null or length(p_identity) not between 1 and 255 or p_identity ~ '[[:space:][:cntrl:]]' then return null; end if;
 select * into a from jobflow_private.accounts where email=p_email for update;
 if not found or a.status not in ('invited','active') or a.migration_pending then return null; end if;
 if a.identity_id is not null and a.identity_id <> p_identity then return null; end if;
 if exists(select 1 from jobflow_private.accounts where identity_id=p_identity and id<>a.id) then return null; end if;
 update jobflow_private.accounts set identity_id=p_identity,status='active' where id=a.id returning * into a;
 return jobflow_private.account_json(a);
exception when unique_violation then return null;
end $$;
create or replace function public.jobflow_account_complete(p_account uuid)
returns jsonb language plpgsql security invoker set search_path='' as $$
declare a jobflow_private.accounts;
begin
 update jobflow_private.accounts set onboarding_complete=true where id=p_account and status in ('active','invited') and not migration_pending returning * into a;
 if not found then raise exception 'Account unavailable'; end if;
 return jobflow_private.account_json(a);
end $$;
create or replace function public.jobflow_account_begin_adoption(p_account uuid,p_source_id text)
returns boolean language plpgsql security invoker set search_path='' as $$
declare a jobflow_private.accounts;
begin
 if p_source_id is null or p_source_id !~ '^[0-9a-f]{64}$' then raise exception 'Invalid migration fingerprint'; end if;
 select * into a from jobflow_private.accounts where id=p_account for update;
 if not found then raise exception 'Invite the account before importing'; end if;
 if a.migration_source_id is not null and a.migration_source_id<>p_source_id then raise exception 'Another migration already owns this workspace'; end if;
 if a.adopted_at is not null then return false; end if;
 if a.status <> 'invited' or a.identity_id is not null then raise exception 'Existing signed-in accounts cannot be replaced by an import'; end if;
 update jobflow_private.accounts set migration_pending=true,migration_source_id=p_source_id where id=a.id;
 return true;
end $$;
create or replace function public.jobflow_account_adopted(p_account uuid,p_adopted_at timestamptz,p_source_id text)
returns jsonb language plpgsql security invoker set search_path='' as $$
declare a jobflow_private.accounts;
begin
 select * into a from jobflow_private.accounts where id=p_account for update;
 if not found or a.status not in ('active','invited') or a.migration_source_id is distinct from p_source_id or p_adopted_at is null then return null; end if;
 if a.adopted_at is not null then return jobflow_private.account_json(a); end if;
 if not a.migration_pending then return null; end if;
 update jobflow_private.accounts set adopted_at=p_adopted_at,onboarding_complete=true,migration_pending=false where id=a.id returning * into a;
 return jobflow_private.account_json(a);
end $$;

create or replace function public.jobflow_session_create(p_account uuid,p_token_hash text,p_csrf text,p_seconds integer default 43200)
returns boolean language plpgsql security invoker set search_path='' as $$
declare a jobflow_private.accounts; v_now double precision=extract(epoch from clock_timestamp());
begin
 if p_seconds not between 1 and 43200 then raise exception 'Invalid session duration'; end if;
 select * into a from jobflow_private.accounts where id=p_account for share;
 if not found or a.status<>'active' or a.identity_id is null or a.migration_pending then return false; end if;
 delete from jobflow_private.sessions where expires_at<=v_now;
 insert into jobflow_private.sessions values(p_token_hash,p_account,p_csrf,v_now,v_now+p_seconds);
 return true;
end $$;
create or replace function public.jobflow_session_get(p_token_hash text)
returns jsonb language sql stable security invoker set search_path='' as $$
 select jsonb_build_object('account',jobflow_private.account_json(a),'csrf_token',s.csrf_token)
 from jobflow_private.sessions s join jobflow_private.accounts a on a.id=s.account_id
 where s.token_hash=p_token_hash and s.expires_at>extract(epoch from now()) and a.status='active' and not a.migration_pending;
$$;
create or replace function public.jobflow_session_revoke(p_token_hash text)
returns void language sql security invoker set search_path='' as $$
 delete from jobflow_private.sessions where token_hash=p_token_hash;
$$;

create or replace function public.jobflow_pending_start(p_token_hash text,p_old_hash text,p_email text,p_verifier text,p_seconds integer default 600,p_maximum integer default 10000)
returns void language plpgsql security invoker set search_path='' as $$
declare v_now double precision=extract(epoch from clock_timestamp()); v_excess integer;
begin
 if p_seconds not between 1 and 600 or p_maximum not between 1 and 10000 or length(p_email)>254 then raise exception 'Invalid sign-in binding'; end if;
 perform pg_advisory_xact_lock(431902,1);
 delete from jobflow_private.pending_signins where token_hash=p_old_hash or expires_at<=v_now;
 insert into jobflow_private.pending_signins values(p_token_hash,p_email,p_verifier,v_now,v_now+p_seconds);
 select greatest(count(*)-p_maximum,0)::integer into v_excess from jobflow_private.pending_signins;
 if v_excess>0 then delete from jobflow_private.pending_signins where token_hash in (
  select token_hash from jobflow_private.pending_signins order by created_at,token_hash limit v_excess); end if;
end $$;
create or replace function public.jobflow_pending_consume(p_token_hash text)
returns jsonb language plpgsql security invoker set search_path='' as $$
declare p jobflow_private.pending_signins;
begin
 delete from jobflow_private.pending_signins where token_hash=p_token_hash returning * into p;
 if not found or p.expires_at<=extract(epoch from clock_timestamp()) then return null; end if;
 return jsonb_build_object('email',p.email,'verifier',p.verifier);
end $$;
create or replace function public.jobflow_pending_revoke(p_token_hash text)
returns void language sql security invoker set search_path='' as $$
 delete from jobflow_private.pending_signins where token_hash=p_token_hash;
$$;
create or replace function public.jobflow_rate_allow(p_key_hash text,p_limit integer,p_window integer,p_maximum integer default 10000)
returns boolean language plpgsql security invoker set search_path='' as $$
declare v_now double precision=extract(epoch from clock_timestamp()); n integer;
begin
 if p_limit not between 1 and 10000 or p_window not between 1 and 86400 or p_maximum not between 1 and 10000 then raise exception 'Invalid rate limit'; end if;
 perform pg_advisory_xact_lock(431902,2);
 delete from jobflow_private.rate_limits where expires_at<=v_now;
 insert into jobflow_private.rate_limits values(p_key_hash,1,v_now,v_now+p_window)
 on conflict(key_hash) do update set counter=jobflow_private.rate_limits.counter+1 returning counter into n;
 delete from jobflow_private.rate_limits where key_hash in (
  select key_hash from jobflow_private.rate_limits order by expires_at desc,key_hash offset p_maximum);
 return n<=p_limit;
end $$;

create or replace function public.jobflow_queue_enqueue(p_account uuid,p_command text default 'run')
returns boolean language plpgsql security invoker set search_path='' as $$
declare w jobflow_private.work; inserted integer; v_now double precision=extract(epoch from clock_timestamp()); followup text;
begin
 if jobflow_private.command_priority(p_command)<0 then raise exception 'Invalid background command'; end if;
 if not exists(select 1 from jobflow_private.accounts where id=p_account and status in ('active','invited') and not migration_pending) then raise exception 'Account unavailable'; end if;
 insert into jobflow_private.work(account_id,status,command,requested_at,available_at)
 values(p_account,'pending',p_command,v_now,v_now) on conflict(account_id) do nothing;
 get diagnostics inserted=row_count;
 if inserted>0 then return true; end if;
 select * into w from jobflow_private.work where account_id=p_account for update;
 if w.status='pending' then
  if jobflow_private.command_priority(p_command)<=jobflow_private.command_priority(w.command) then return false; end if;
  update jobflow_private.work set command=p_command,available_at=v_now,attempts=0,message='' where account_id=p_account;
  return true;
 elsif w.status='running' then
  if p_command='due' then return false; end if;
  followup=case when jobflow_private.command_priority(p_command)>jobflow_private.command_priority(w.next_command) then p_command else w.next_command end;
  if followup is not distinct from w.next_command then return false; end if;
  update jobflow_private.work set next_command=followup where account_id=p_account;
  return true;
 end if;
 update jobflow_private.work set status='pending',command=p_command,requested_at=v_now,available_at=v_now,
  attempts=0,started_at=null,finished_at=null,lease_until=null,claim_token=null,next_command=null,message='' where account_id=p_account;
 return true;
end $$;
create or replace function public.jobflow_queue_status(p_account uuid)
returns jsonb language sql stable security invoker set search_path='' as $$
 select jsonb_build_object('status',status,'command',command,'next_command',next_command,
  'requested_at',requested_at,'started_at',started_at,'finished_at',finished_at,'message',message)
 from jobflow_private.work where account_id=p_account;
$$;
create or replace function public.jobflow_queue_claim(p_token text)
returns jsonb language plpgsql security invoker set search_path='' as $$
declare w jobflow_private.work; v_now double precision=extract(epoch from clock_timestamp()); v_next_command text;
begin
 if p_token is null or p_token !~ '^[0-9a-f]{32}$' then raise exception 'Invalid queue claim'; end if;
 for w in select * from jobflow_private.work where status='running' and lease_until<=v_now for update skip locked loop
  if w.attempts>=3 and w.next_command is null then
   update jobflow_private.work set status='failed',finished_at=v_now,lease_until=null,claim_token=null,
    message='Background work was interrupted repeatedly. Try again.' where account_id=w.account_id;
  else
   v_next_command=case when jobflow_private.command_priority(w.next_command)>jobflow_private.command_priority(w.command) then w.next_command else w.command end;
   update jobflow_private.work set status='pending',command=v_next_command,available_at=v_now,
    attempts=case when w.next_command is not null then 0 else w.attempts end,
    lease_until=null,claim_token=null,next_command=null where account_id=w.account_id;
  end if;
 end loop;
 select * into w from jobflow_private.work where status='pending' and available_at<=v_now
  order by requested_at,account_id limit 1 for update skip locked;
 if not found then return null; end if;
 update jobflow_private.work set status='running',started_at=v_now,lease_until=v_now+1900,
  attempts=attempts+1,claim_token=p_token where account_id=w.account_id returning * into w;
 return to_jsonb(w);
end $$;
create or replace function public.jobflow_queue_finish(p_account uuid,p_token text,p_status text,p_message text)
returns boolean language plpgsql security invoker set search_path='' as $$
declare w jobflow_private.work; v_now double precision=extract(epoch from clock_timestamp()); command_next text;
begin
 if p_status not in ('retry','done','partial','failed') or p_message is null or length(p_message)>1000 then raise exception 'Invalid background result'; end if;
 select * into w from jobflow_private.work where account_id=p_account and claim_token=p_token and status='running' and lease_until>v_now for update;
 if not found then return false; end if;
 if p_status='retry' and w.attempts<3 then
  command_next=case when jobflow_private.command_priority(w.next_command)>jobflow_private.command_priority(w.command) then w.next_command else w.command end;
  update jobflow_private.work set status='pending',command=command_next,attempts=case when command_next=w.command then w.attempts else 0 end,
   message=p_message,finished_at=null,available_at=v_now+60,lease_until=null,claim_token=null,next_command=null where account_id=p_account;
 elsif w.next_command is not null then
  update jobflow_private.work set status='pending',command=w.next_command,next_command=null,attempts=0,
   requested_at=v_now,started_at=null,finished_at=null,available_at=v_now,lease_until=null,claim_token=null,
   message='A newer workspace update is queued.' where account_id=p_account;
 else
  update jobflow_private.work set status=case when p_status='retry' then 'failed' else p_status end,
   message=p_message,finished_at=v_now,lease_until=null,claim_token=null where account_id=p_account;
 end if;
 return true;
end $$;
create or replace function public.jobflow_queue_renew(p_account uuid,p_token text)
returns boolean language plpgsql security invoker set search_path='' as $$
declare v_now double precision=extract(epoch from clock_timestamp());
begin
 update jobflow_private.work set lease_until=v_now+1900 where account_id=p_account and claim_token=p_token and status='running' and lease_until>v_now;
 return found;
end $$;

-- PostgreSQL grants EXECUTE to PUBLIC by default: explicitly close every RPC.
do $$ declare fn record; begin
 for fn in select p.oid::regprocedure as signature from pg_proc p join pg_namespace n on n.oid=p.pronamespace
  where n.nspname='public' and (p.proname like 'jobflow_account_%' or p.proname like 'jobflow_session_%' or
   p.proname like 'jobflow_pending_%' or p.proname like 'jobflow_queue_%' or p.proname='jobflow_rate_allow')
 loop
  execute format('revoke all on function %s from public, anon, authenticated',fn.signature);
  execute format('grant execute on function %s to service_role',fn.signature);
 end loop;
end $$;
