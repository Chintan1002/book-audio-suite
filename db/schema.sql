-- ============================================================================
-- Book/Audio Suite — multi-user schema
--
-- Target: Supabase (Postgres 15+). Runs on plain Postgres too; the only
-- Supabase-specific dependency is `auth.users`, which the shim below creates
-- when absent so local dev works without a Supabase connection.
--
-- SCOPE, deliberately narrow:
--   * STT (audio -> text) is the feature with a library. Transcripts are saved
--     permanently, per user, and are searchable.
--   * TTS (PDF -> audiobook) saves NOTHING. It stays a transient tool: upload,
--     download, done. No audiobook, no book text, no uploaded PDF is retained.
--
-- Consequence worth stating plainly: there are no stored files anywhere in this
-- schema. Every transcript download format (.txt/.md/.json/.srt/.vtt) is
-- rendered on demand from `transcripts.content` and `transcripts.segments`, so
-- there is no object storage, no signed URLs, no expiry sweeper, and no
-- byte-based quota to maintain.
--
-- Apply:  psql "$DATABASE_URL" -f db/schema.sql
--         (or paste into the Supabase SQL editor)
-- ============================================================================

create extension if not exists "pgcrypto";    -- gen_random_uuid()

-- ------------------------------------------------------- local-dev auth shim
do $$
begin
  if not exists (select 1 from pg_namespace where nspname = 'auth') then
    create schema auth;
    create table auth.users (
      id    uuid primary key default gen_random_uuid(),
      email text unique
    );
    -- Supabase exposes the caller's id as auth.uid(); stub it for local runs.
    create or replace function auth.uid() returns uuid
      language sql stable as $fn$
        select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid
      $fn$;
  end if;
end $$;

-- --------------------------------------------------------------------- enums
do $$ begin create type job_kind   as enum ('tts', 'stt');                      exception when duplicate_object then null; end $$;
do $$ begin create type job_status as enum ('queued','running','done','error'); exception when duplicate_object then null; end $$;


-- ============================================================================
-- profiles — one row per user; identity plus remembered form defaults.
-- ============================================================================
create table if not exists profiles (
  id                   uuid primary key references auth.users(id) on delete cascade,
  display_name         text,

  -- Remembered defaults so a returning user doesn't re-pick every time.
  -- TTS defaults are still stored: the audiobook isn't saved, but the user's
  -- preferences for making one are worth remembering.
  default_voice        text not null default 'en-US-AndrewNeural',
  default_rate         text not null default '-12%',
  default_format       text not null default '.mp3',
  default_stt_model    text not null default 'base',
  default_stt_language text,                     -- null = auto-detect

  -- Fair-use limit. With no files stored, the scarce resource is CPU time, not
  -- disk — so the quota counts transcribed audio, not bytes.
  monthly_minutes_quota int not null default 600,

  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now()
);

-- Create a profile automatically whenever a new auth user appears.
create or replace function handle_new_user() returns trigger
  language plpgsql security definer set search_path = public as $$
begin
  insert into public.profiles (id, display_name)
  values (new.id, split_part(coalesce(new.email, ''), '@', 1))
  on conflict (id) do nothing;
  return new;
end $$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function handle_new_user();


-- ============================================================================
-- jobs — in-flight processing state for BOTH features.
--
-- This is today's in-memory Job dataclass made durable, so the polling contract
-- (`GET /jobs/{id}`) is unchanged and a restart no longer strands a job.
--
-- TTS rows are working state only: once the user has downloaded their
-- audiobook the row has no lasting value and is pruned (see prune_stale_jobs).
-- STT rows produce a transcript, which is the thing that lasts.
-- ============================================================================
create table if not exists jobs (
  id              uuid primary key default gen_random_uuid(),
  user_id         uuid not null references auth.users(id) on delete cascade,
  kind            job_kind   not null,
  status          job_status not null default 'queued',
  stage           text,                          -- 'extract' | 'transcribe' | ...
  progress        real not null default 0 check (progress between 0 and 1),
  error           text,
  title           text not null,
  source_filename text,
  -- Run configuration (voice/rate/format, or model/language). jsonb keeps new
  -- options from needing a migration each time one is added.
  params          jsonb not null default '{}'::jsonb,
  -- Output locations produced by the run (local paths for TTS working files).
  result          jsonb not null default '{}'::jsonb,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  completed_at    timestamptz
);

create index if not exists jobs_user_created_idx on jobs (user_id, created_at desc);
create index if not exists jobs_active_idx on jobs (status)
  where status in ('queued', 'running');


-- ============================================================================
-- transcripts — THE LIBRARY. The only durable user content in the product.
--
-- `content`  the readable, paragraph-formatted transcript
-- `segments` the timestamped structure: [{start, end, text, words[]}]
--            This is what lets every download format be generated on demand:
--              .txt / .md  <- content (+ metadata header)
--              .json       <- segments
--              .srt / .vtt <- segments
--            Storing the source of truth instead of five rendered files means a
--            new format added later works for every existing transcript.
-- ============================================================================
create table if not exists transcripts (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid not null references auth.users(id) on delete cascade,
  job_id           uuid references jobs(id) on delete set null,

  title            text not null,
  content          text not null,
  segments         jsonb not null default '[]'::jsonb,

  language         text,                         -- detected or forced
  model            text,                         -- tiny | base | small | medium
  duration_seconds real,                         -- length of the source audio
  word_count       int,
  source_filename  text,                         -- what they uploaded (name only)

  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);

create index if not exists transcripts_user_created_idx
  on transcripts (user_id, created_at desc);

-- Full-text search across a user's own library.
alter table transcripts
  add column if not exists search_vector tsvector
  generated always as (
    to_tsvector('english', coalesce(title, '') || ' ' || coalesce(content, ''))
  ) stored;
create index if not exists transcripts_search_idx
  on transcripts using gin (search_vector);


-- ============================================================================
-- Usage accounting — the quota is minutes of audio, not megabytes.
-- ============================================================================
create or replace view user_usage with (security_invoker = true) as
  select
    p.id                     as user_id,
    p.monthly_minutes_quota,
    coalesce((select sum(t.duration_seconds) / 60.0
                from transcripts t
               where t.user_id = p.id
                 and t.created_at >= date_trunc('month', now())), 0)::numeric(10,1)
                             as minutes_this_month,
    (select count(*) from transcripts t where t.user_id = p.id)
                             as transcript_count
  from profiles p;


-- ============================================================================
-- Row Level Security — the isolation guarantee.
-- With these policies a client query can only ever return the caller's own
-- rows, even if the application layer has a bug. This is why one shared
-- database is sufficient and a database-per-user is unnecessary.
-- ============================================================================
alter table profiles    enable row level security;
alter table jobs        enable row level security;
alter table transcripts enable row level security;

drop policy if exists profiles_select_own on profiles;
create policy profiles_select_own on profiles for select using (auth.uid() = id);
drop policy if exists profiles_update_own on profiles;
create policy profiles_update_own on profiles for update using (auth.uid() = id);

drop policy if exists jobs_all_own on jobs;
create policy jobs_all_own on jobs
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

drop policy if exists transcripts_all_own on transcripts;
create policy transcripts_all_own on transcripts
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- NOTE: the background worker connects as the service role, which bypasses RLS
-- by design. RLS stops a careless *client* query, not a careless server-side
-- one — every worker query must still filter by user_id explicitly.


-- ============================================================================
-- Maintenance
-- ============================================================================
create or replace function touch_updated_at() returns trigger
  language plpgsql as $$
begin new.updated_at = now(); return new; end $$;

drop trigger if exists jobs_touch on jobs;
create trigger jobs_touch
  before update on jobs for each row execute function touch_updated_at();
drop trigger if exists transcripts_touch on transcripts;
create trigger transcripts_touch
  before update on transcripts for each row execute function touch_updated_at();
drop trigger if exists profiles_touch on profiles;
create trigger profiles_touch
  before update on profiles for each row execute function touch_updated_at();

-- Jobs are working state. Completed TTS jobs carry nothing durable, and old
-- finished STT jobs are superseded by their transcript, so both are prunable.
-- A job still queued or running is never touched, regardless of age.
create or replace function prune_stale_jobs(older_than interval default interval '7 days')
  returns integer language sql as $$
    with deleted as (
      delete from jobs
       where status in ('done', 'error')
         and coalesce(completed_at, updated_at) < now() - older_than
      returning 1
    )
    select count(*)::int from deleted
  $$;

-- Jobs left mid-run by a crash or redeploy. Call once on startup so the UI
-- never shows a job that will never finish.
create or replace function fail_interrupted_jobs()
  returns integer language sql as $$
    with updated as (
      update jobs
         set status = 'error',
             error  = 'Interrupted by a server restart. Please run it again.',
             completed_at = now()
       where status in ('queued', 'running')
      returning 1
    )
    select count(*)::int from updated
  $$;
