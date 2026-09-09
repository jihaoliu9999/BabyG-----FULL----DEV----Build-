alter table public.oauth_connections
  drop constraint if exists oauth_connections_provider_check;

alter table public.oauth_connections
  add constraint oauth_connections_provider_check
  check (provider in ('google', 'instagram'));;
