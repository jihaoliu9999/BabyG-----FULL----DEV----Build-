alter table public.creator_discovery_actions
  drop constraint if exists creator_discovery_actions_action_type_check;

alter table public.creator_discovery_actions
  add constraint creator_discovery_actions_action_type_check
  check (action_type in (
    'viewed',
    'passed',
    'connected',
    'skipped',
    'opened_profile',
    'undo_pass'
  ));

alter table public.creator_connections
  drop constraint if exists creator_connections_status_check;

alter table public.creator_connections
  add constraint creator_connections_status_check
  check (status in (
    'pending',
    'accepted',
    'declined',
    'blocked',
    'removed'
  ));
;
