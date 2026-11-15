-- Execute after migrations in a disposable/test transaction. All rows roll back.
begin;
set local role service_role;
do $$
declare
 a jsonb; b jsonb; id_a uuid; id_b uuid; identity_a text=gen_random_uuid()::text;
 s jsonb; item jsonb; item2 jsonb; denied boolean=false;
 email_a text='registry-test-'||gen_random_uuid()::text||'@example.test';
 email_b text='registry-test-'||gen_random_uuid()::text||'@example.test';
begin
 if has_function_privilege('anon','public.jobflow_account_invite(text)','EXECUTE')
    or has_function_privilege('authenticated','public.jobflow_account_invite(text)','EXECUTE')
    or has_function_privilege('anon','public.jobflow_session_get(text)','EXECUTE')
    or has_function_privilege('authenticated','public.jobflow_queue_claim(text)','EXECUTE') then
  raise exception 'Browser roles can execute private RPCs';
 end if;
 if has_schema_privilege('anon','jobflow_private','USAGE') or has_table_privilege('authenticated','jobflow_private.accounts','SELECT') then
  raise exception 'Browser roles can access private account tables';
 end if;
 if exists(select 1 from pg_class c join pg_namespace n on c.relnamespace=n.oid
           where n.nspname='jobflow_private' and c.relname in ('accounts','sessions','pending_signins','rate_limits','work') and not c.relrowsecurity) then
  raise exception 'Private tables must have RLS enabled';
 end if;
 a=public.jobflow_account_invite(email_a); id_a=(a->>'id')::uuid;
 b=public.jobflow_account_invite(email_b); id_b=(b->>'id')::uuid;
 if (public.jobflow_account_invite(email_a)->>'id')::uuid<>id_a then raise exception 'Invitation not idempotent'; end if;
 if public.jobflow_account_authenticate(identity_a,'uninvited@example.test') is not null then raise exception 'Uninvited account authenticated'; end if;
 if public.jobflow_session_create(id_a,repeat('a',64),repeat('x',43),600) then raise exception 'Unbound invitation created a session'; end if;
 a=public.jobflow_account_authenticate(identity_a,email_a);
 if a->>'status'<>'active' then raise exception 'Verified invited identity not bound'; end if;
 if public.jobflow_account_authenticate(identity_a,email_b) is not null then raise exception 'Identity claimed a second account'; end if;
 if public.jobflow_account_authenticate(gen_random_uuid()::text,email_a) is not null then raise exception 'Existing identity replaced'; end if;
 if not public.jobflow_session_create(id_a,repeat('a',64),repeat('x',43),600) then raise exception 'Active account did not create a session'; end if;
 s=public.jobflow_session_get(repeat('a',64));
 if (s->'account'->>'id')::uuid<>id_a or s->>'csrf_token'<>repeat('x',43) then raise exception 'Session routed incorrectly'; end if;
 update jobflow_private.accounts set status='disabled' where id=id_a;
 if public.jobflow_session_get(repeat('a',64)) is not null then raise exception 'Disabled account session remains usable'; end if;
 update jobflow_private.accounts set status='active' where id=id_a;
 perform public.jobflow_session_revoke(repeat('a',64));
 if public.jobflow_session_get(repeat('a',64)) is not null then raise exception 'Revoked session remains usable'; end if;

 if not public.jobflow_account_begin_adoption(id_b,repeat('b',64)) then raise exception 'New owner import did not begin'; end if;
 if public.jobflow_account_authenticate(gen_random_uuid()::text,email_b) is not null then raise exception 'Account signed in during owner import'; end if;
 begin
  perform public.jobflow_account_begin_adoption(id_b,repeat('c',64));
 exception when raise_exception then denied=true;
 end;
 if not denied then raise exception 'Conflicting owner import accepted'; end if;
 b=public.jobflow_account_adopted(id_b,now(),repeat('b',64));
 if b is null or not (b->>'onboarding_complete')::boolean then raise exception 'Owner import not finalized'; end if;
 if public.jobflow_account_begin_adoption(id_b,repeat('b',64)) then raise exception 'Completed owner import allowed replacement'; end if;

 perform public.jobflow_pending_start(repeat('d',64),null,email_a,repeat('v',86),600,10000);
 perform public.jobflow_pending_start(repeat('e',64),repeat('d',64),email_a,repeat('w',86),600,10000);
 if public.jobflow_pending_consume(repeat('d',64)) is not null then raise exception 'Old browser binding was not revoked'; end if;
 s=public.jobflow_pending_consume(repeat('e',64));
 if s->>'email'<>email_a or s->>'verifier'<>repeat('w',86) then raise exception 'PKCE verifier binding incorrect'; end if;
 if public.jobflow_pending_consume(repeat('e',64)) is not null then raise exception 'PKCE binding reused'; end if;
 perform public.jobflow_pending_start(repeat('f',64),null,email_a,repeat('v',86),600,10000);
 update jobflow_private.pending_signins set expires_at=0 where token_hash=repeat('f',64);
 if public.jobflow_pending_consume(repeat('f',64)) is not null then raise exception 'Expired PKCE binding accepted'; end if;
 if not public.jobflow_rate_allow(repeat('1',64),1,900,10000) or public.jobflow_rate_allow(repeat('1',64),1,900,10000) then
  raise exception 'Durable rate limit failed';
 end if;

 if not public.jobflow_queue_enqueue(id_a,'due') or not public.jobflow_queue_enqueue(id_a,'run') then raise exception 'Queue creation/escalation failed'; end if;
 if public.jobflow_queue_enqueue(id_a,'refresh') then raise exception 'Weaker queue request displaced stronger work'; end if;
 -- A test transaction owns only these temporary queue rows in a new project.
 item=public.jobflow_queue_claim(repeat('2',32));
 if item->>'account_id'<>id_a::text or item->>'command'<>'run' then raise exception 'Queue claim returned unexpected work'; end if;
 if public.jobflow_queue_enqueue(id_a,'due') then raise exception 'Schedule created redundant followup'; end if;
 if not public.jobflow_queue_enqueue(id_a,'refresh') then raise exception 'Profile update followup lost'; end if;
 if public.jobflow_queue_finish(id_a,repeat('3',32),'done','stale') then raise exception 'Stale worker completed newer claim'; end if;
 if not public.jobflow_queue_finish(id_a,repeat('2',32),'done','done') then raise exception 'Current worker could not finish'; end if;
 item2=public.jobflow_queue_claim(repeat('4',32));
 if item2->>'command'<>'refresh' then raise exception 'Followup was not retained'; end if;
 update jobflow_private.work set lease_until=0 where account_id=id_a;
 if public.jobflow_queue_finish(id_a,repeat('4',32),'done','expired') then raise exception 'Expired worker completed claim'; end if;
 item=public.jobflow_queue_claim(repeat('5',32));
 if item->>'account_id'<>id_a::text or item->>'claim_token'<>repeat('5',32) then raise exception 'Expired claim not recovered'; end if;
 if public.jobflow_queue_finish(id_a,repeat('4',32),'done','stale') then raise exception 'Old worker won after recovery'; end if;
 if not public.jobflow_queue_finish(id_a,repeat('5',32),'partial','source error') then raise exception 'Partial source result lost'; end if;
 if public.jobflow_queue_status(id_a)->>'status'<>'partial' then raise exception 'Partial source result mislabeled'; end if;
end $$;
rollback;
