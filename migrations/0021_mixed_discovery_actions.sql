-- babyg :: 0021 :: mixed discovery action targets
-- Backfills every existing creator action to target_kind='creator' while
-- allowing brand and opportunity cards to share the same undo/cooldown ledger.

begin;

alter table public.creator_discovery_actions
  add column if not exists target_kind text not null default 'creator',
  add column if not exists target_card_id uuid;

update public.creator_discovery_actions
set target_card_id = target_user_id
where target_card_id is null;

alter table public.creator_discovery_actions
  alter column target_user_id drop not null,
  alter column target_card_id set not null,
  drop constraint if exists creator_discovery_actions_action_type_check,
  drop constraint if exists creator_discovery_actions_target_kind_check,
  drop constraint if exists creator_discovery_actions_target_shape_check,
  drop constraint if exists creator_discovery_actions_check;

alter table public.creator_discovery_actions
  add constraint creator_discovery_actions_action_type_check
    check (action_type in (
      'viewed', 'passed', 'saved', 'connected', 'interested',
      'skipped', 'opened_profile', 'undo_pass'
    )),
  add constraint creator_discovery_actions_target_kind_check
    check (target_kind in ('creator', 'brand', 'opportunity')),
  add constraint creator_discovery_actions_target_shape_check
    check (
      user_id <> target_user_id
      and (
        (target_kind in ('creator', 'brand') and target_user_id = target_card_id)
        or (target_kind = 'opportunity' and target_user_id is not null)
      )
    );

create index if not exists idx_creator_discovery_mixed_target
  on public.creator_discovery_actions(user_id, target_kind, target_card_id, created_at desc);

commit;
