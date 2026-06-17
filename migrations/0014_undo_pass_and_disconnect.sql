-- 0014_undo_pass_and_disconnect.sql
--
-- Two small enum-widening changes for the network/swipe features:
--
--   1. creator_discovery_actions: add the `undo_pass` action type so a
--      viewer can undo a recent pass. The discovery service treats a
--      creator as "passed" only while their most recent pass/undo
--      action is `passed`; an `undo_pass` restores them to the stack.
--
--   2. creator_connections: add the `removed` status so a creator can
--      disconnect from an accepted connection without blocking the peer
--      or deleting any profile/message history. `list_accepted_for_user`
--      keeps filtering on `accepted`, so removed rows drop off the
--      connections list, and discovery excludes `removed` peers from the
--      swipe stack (no instant rediscovery).
--
-- Both CHECK constraints are recreated to include the new value. RLS,
-- columns, and indexes are unchanged.

begin;

-- 1. discovery action types ---------------------------------------------------
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

-- 2. connection statuses ------------------------------------------------------
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

commit;
