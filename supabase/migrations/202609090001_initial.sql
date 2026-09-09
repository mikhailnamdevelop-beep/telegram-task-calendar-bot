-- Telegram task/calendar MVP. Run through Supabase migrations as an owner role.
-- Access is server-side only: RLS has no public/anon/authenticated policies.
begin;

create table public.items (
    id uuid primary key default gen_random_uuid(),
    short_id text not null unique default left(replace(gen_random_uuid()::text, '-', ''), 8),
    telegram_user_id bigint not null check (telegram_user_id > 0),
    telegram_chat_id bigint not null,
    telegram_message_id bigint check (telegram_message_id > 0),
    item_type text not null check (item_type in ('task', 'calendar', 'both')),
    title text not null check (length(btrim(title)) between 1 and 500),
    scheduled_date date not null,
    start_time time without time zone,
    duration_minutes integer check (duration_minutes between 1 and 10080),
    timezone text not null default 'Asia/Seoul' check (length(timezone) between 1 and 100),
    status text not null default 'active'
        check (status in ('active', 'done', 'cancelled', 'sync_pending', 'calendar_created', 'sync_failed')),
    google_event_id text,
    google_event_html_link text,
    version bigint not null default 1 check (version >= 1),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    deleted_at timestamptz,
    constraint items_short_id_format check (short_id ~ '^[A-Za-z0-9_-]{4,32}$'),
    constraint items_calendar_time_required
        check (item_type = 'task' or (start_time is not null and duration_minutes is not null)),
    constraint items_deleted_status check (deleted_at is null or status = 'cancelled')
);

-- Telegram redelivery of the same message cannot create a second item.
create unique index items_telegram_message_unique
    on public.items (telegram_chat_id, telegram_message_id)
    where telegram_message_id is not null;
create index items_user_schedule_idx
    on public.items (telegram_user_id, scheduled_date, start_time, id)
    where deleted_at is null;
create index items_user_status_idx
    on public.items (telegram_user_id, status, scheduled_date)
    where deleted_at is null;
create index items_calendar_reconciliation_idx
    on public.items (updated_at)
    where status in ('sync_pending', 'sync_failed');

create table public.dialog_states (
    telegram_chat_id bigint not null,
    telegram_user_id bigint not null check (telegram_user_id > 0),
    payload jsonb not null default '{}'::jsonb check (jsonb_typeof(payload) = 'object'),
    version bigint not null default 1 check (version >= 1),
    expires_at timestamptz not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (telegram_chat_id, telegram_user_id)
);
create index dialog_states_expires_at_idx on public.dialog_states (expires_at);

create table public.operations (
    id uuid primary key default gen_random_uuid(),
    idempotency_key text not null unique check (length(idempotency_key) between 1 and 200),
    telegram_user_id bigint not null check (telegram_user_id > 0),
    telegram_chat_id bigint,
    item_id uuid references public.items (id) on delete restrict,
    operation_type text not null check (operation_type in ('create', 'add', 'update', 'edit', 'delete', 'done', 'agenda', 'undo')),
    status text not null default 'pending' check (status in ('pending', 'succeeded', 'failed')),
    error_code text,
    error_message text,
    payload jsonb not null default '{}'::jsonb check (jsonb_typeof(payload) = 'object'),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create index operations_item_idx on public.operations (item_id, created_at desc);
create index operations_user_idx on public.operations (telegram_user_id, created_at desc);
create index operations_retry_idx on public.operations (updated_at) where status in ('pending', 'failed');

-- Repositories must additionally UPDATE ... WHERE id = ? AND version = expected.
-- Optimistic locking is enforced by repository UPDATE ... WHERE version = expected.
-- The trigger owns the increment so regular updates and PostgREST upserts agree.
create function public.bump_record_version()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    new.version := old.version + 1;
    new.updated_at := now();
    new.created_at := old.created_at;
    return new;
end;
$$;

create function public.touch_operation_timestamp()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    new.updated_at := now();
    new.created_at := old.created_at;
    return new;
end;
$$;

create trigger items_bump_version before update on public.items
    for each row execute function public.bump_record_version();
create trigger dialog_states_bump_version before update on public.dialog_states
    for each row execute function public.bump_record_version();
create trigger operations_touch_timestamp before update on public.operations
    for each row execute function public.touch_operation_timestamp();

alter table public.items enable row level security;
alter table public.dialog_states enable row level security;
alter table public.operations enable row level security;

revoke all on public.items, public.dialog_states, public.operations from anon, authenticated;
grant select, insert, update, delete on public.items, public.dialog_states, public.operations to service_role;
revoke all on function public.bump_record_version() from public;
revoke all on function public.touch_operation_timestamp() from public;

commit;
