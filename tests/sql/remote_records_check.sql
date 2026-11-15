-- Record/run/lease checks with synthetic data; every mutation is rolled back.
begin;
set local role service_role;
do $$
declare
 w uuid=gen_random_uuid(); other uuid=gen_random_uuid();
 token text=repeat('a',43); token2 text=repeat('b',43);
 rows jsonb; item jsonb; page jsonb; rev bigint; run_id bigint; denied boolean;
 created text='2026-01-02T03:04:05.123456+00:00';
 updated text='2026-02-03T04:05:06.123456+00:00';
begin
 if exists(select 1 from pg_proc p join pg_namespace n on n.oid=p.pronamespace
   where n.nspname='public' and p.proname in (
     'jobflow_workspace_create','jobflow_workspace_lease','jobflow_record_get',
     'jobflow_records_page','jobflow_records_put','jobflow_run_start','jobflow_run_finish',
     'jobflow_runs_recent','jobflow_runs_import','jobflow_workspace_stats','jobflow_import_verify')
   and (has_function_privilege('anon',p.oid,'EXECUTE') or has_function_privilege('authenticated',p.oid,'EXECUTE')
        or p.prosecdef)) then
   raise exception 'Record RPCs must be invoker-only and unavailable to browser roles';
 end if;
 if has_schema_privilege('anon','jobflow_private','USAGE')
    or has_schema_privilege('authenticated','jobflow_private','USAGE') then
   raise exception 'Private schema is exposed to browser roles';
 end if;
 if exists(select 1 from pg_class c join pg_namespace n on c.relnamespace=n.oid
   where n.nspname='jobflow_private' and c.relname in ('workspaces','records','runs') and not c.relrowsecurity) then
   raise exception 'Private record tables need RLS';
 end if;
 perform public.jobflow_workspace_create(w);
 perform public.jobflow_workspace_create(other);
 if not public.jobflow_workspace_lease(w,token,60,'acquire') then raise exception 'Lease acquisition failed'; end if;
 if public.jobflow_workspace_lease(w,token2,60,'acquire') then raise exception 'Concurrent lease acquired'; end if;
 if not public.jobflow_workspace_lease(other,token2,60,'acquire') then raise exception 'Separate workspace lease failed'; end if;
 rows=jsonb_build_array(
   jsonb_build_object('kind','jobs','id','matching','body',jsonb_build_object('id','matching','eligible',true,'score',50,'description','preserved full description'),'created_at',created,'updated_at',updated),
   jsonb_build_object('kind','jobs','id','tracked','body',jsonb_build_object('id','tracked','eligible',false,'score',0,'description','tracked description'),'created_at',created,'updated_at',updated),
   jsonb_build_object('kind','jobs','id','hidden','body',jsonb_build_object('id','hidden','eligible',false,'score',0,'description','hidden description'),'created_at',created,'updated_at',updated),
   jsonb_build_object('kind','applications','id','saved','body',jsonb_build_object('id','saved','job_id','tracked','stage','saved'),'created_at',created,'updated_at',updated));
 denied=false;
 begin
   perform public.jobflow_records_put(w,token2,rows,true);
 exception when lock_not_available then denied=true;
 end;
 if not denied then raise exception 'Another workspace token wrote records'; end if;
 if public.jobflow_records_put(w,token,rows,true)<>4 then raise exception 'Import did not save all rows'; end if;
 if public.jobflow_records_put(w,token,rows,true)<>4 then raise exception 'Identical retry failed'; end if;
 if (public.jobflow_import_verify(w,rows,'[]')->>'records_mismatched')::integer<>0 then raise exception 'Import changed body or timestamp'; end if;
 if public.jobflow_record_get(other,'jobs','matching') is not null then raise exception 'Record leaked across workspace'; end if;
 page=public.jobflow_records_page(w,p_mode=>'matching');
 if (page->>'count')::integer<>3 then raise exception 'Stored job count incorrect'; end if;
 if (select count(*) from jsonb_array_elements(page->'records') r where r->>'kind'='jobs')<>2 then
   raise exception 'Matching/tracked projection incorrect';
 end if;
 if exists(select 1 from jsonb_array_elements(page->'records') r where r->>'kind'='jobs' and r->'body' ? 'description') then
   raise exception 'Summary projection included descriptions';
 end if;
 if public.jobflow_record_get(w,'jobs','matching')->>'description'<>'preserved full description' then
   raise exception 'Projection altered canonical description';
 end if;
 page=public.jobflow_records_page(w,p_kind=>'jobs',p_mode=>'detail',p_job=>'hidden');
 if jsonb_array_length(page->'records')<>1 or page->'records'->0->'body'->>'description'<>'hidden description' then
   raise exception 'Detail projection failed';
 end if;
 page=public.jobflow_records_page(w,p_kind=>'jobs',p_mode=>'ids');
 if exists(select 1 from jsonb_array_elements(page->'records') r where r->'body'<>jsonb_build_object('id',r->>'id')) then
   raise exception 'ID projection included more than IDs';
 end if;
 page=public.jobflow_records_page(w,p_limit=>1);
 rev=(page->>'revision')::bigint;
 item=rows->0;
 item=jsonb_set(item,'{body,score}','70');
 item=jsonb_set(item,'{created_at}','"later-created"');
 item=jsonb_set(item,'{updated_at}','"later-updated"');
 perform public.jobflow_records_put(w,token,jsonb_build_array(item),false);
 if (select created_at from jobflow_private.records where workspace_id=w and kind='jobs' and id='matching')<>created then
   raise exception 'Ordinary update overwrote creation history';
 end if;
 page=public.jobflow_records_page(w,p_revision=>rev);
 if not (page->>'conflict')::boolean or jsonb_array_length(page->'records')<>0 then
   raise exception 'Changed revision returned a mixed snapshot';
 end if;
 denied=false;
 begin
   perform public.jobflow_records_put(w,token,rows,true);
 exception when invalid_parameter_value then denied=true;
 end;
 if not denied then raise exception 'Owner import overwrote newer user edits'; end if;
 if public.jobflow_record_get(w,'jobs','matching')->>'score'<>'70' then raise exception 'Conflicting import changed user data'; end if;
 if (public.jobflow_import_verify(w,rows,'[]')->>'records_mismatched')::integer<>1 then raise exception 'Import mismatch not detected'; end if;

 perform public.jobflow_runs_import(w,token,jsonb_build_array(jsonb_build_object(
   'id',7,'started_at',created,'finished_at',updated,'status','partial','details','{"source_errors":1}')));
 run_id=public.jobflow_run_start(w,token,updated);
 if run_id<>8 then raise exception 'New run did not preserve imported sequence'; end if;
 perform public.jobflow_run_finish(w,token,run_id,updated,'completed','{"preserved":true}');
 page=public.jobflow_runs_recent(w,10);
 if jsonb_array_length(page)<>2 or (page->0->>'id')::integer<>8 or page->0->>'status'<>'completed' then
   raise exception 'Run history incorrect';
 end if;
 if (public.jobflow_workspace_stats(w)->>'runs')::integer<>2 then raise exception 'Run count incorrect'; end if;
 if (public.jobflow_import_verify(w,'[]',jsonb_build_array(jsonb_build_object(
   'id',7,'started_at',created,'finished_at',updated,'status','partial','details','{"source_errors":1}')))->>'runs_mismatched')::integer<>0 then
   raise exception 'Imported run details or timestamps changed';
 end if;
 update jobflow_private.workspaces set lease_until=clock_timestamp()-interval '1 second' where id=w;
 denied=false;
 begin
   perform public.jobflow_records_put(w,token,jsonb_build_array(item),false);
 exception when lock_not_available then denied=true;
 end;
 if not denied then raise exception 'Expired token wrote records'; end if;
 if public.jobflow_workspace_lease(w,token,60,'renew') then raise exception 'Expired lease renewed'; end if;
 if not public.jobflow_workspace_lease(w,token2,60,'acquire') then raise exception 'Expired lease was not recovered'; end if;
 if public.jobflow_workspace_lease(w,token,60,'release') then raise exception 'Stale token released current lease'; end if;
 if not public.jobflow_workspace_lease(w,token2,60,'release') then raise exception 'Current lease did not release'; end if;
end $$;
set local role anon;
do $$
declare denied boolean=false;
begin
 begin perform public.jobflow_record_get(gen_random_uuid(),'jobs','x');
 exception when insufficient_privilege then denied=true; end;
 if not denied then raise exception 'Anonymous role executed private read'; end if;
end $$;
set local role authenticated;
do $$
declare denied boolean=false;
begin
 begin perform public.jobflow_workspace_create(gen_random_uuid());
 exception when insufficient_privilege then denied=true; end;
 if not denied then raise exception 'Authenticated role executed private mutation'; end if;
end $$;
rollback;
