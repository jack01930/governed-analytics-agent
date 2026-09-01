create extension if not exists vector;
revoke create on schema public from public;

do $$
begin
  execute format('revoke temporary on database %I from public', current_database());
end
$$;

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'analytics_loader') then
    create role analytics_loader login password 'analytics_loader_dev' nosuperuser nocreatedb nocreaterole;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'analytics_readonly') then
    create role analytics_readonly login password 'analytics_readonly_dev' nosuperuser nocreatedb nocreaterole;
  end if;
end
$$;

grant usage on schema public to analytics_loader, analytics_readonly;

do $$
begin
  execute format(
    'grant connect on database %I to analytics_loader, analytics_readonly',
    current_database()
  );
end
$$;
