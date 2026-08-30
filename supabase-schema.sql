-- Whoop-tracker: eigen tabel in je gedeelde Supabase-project.
-- Zelfde login als je uren- en finance-apps, eigen whoop_*-tabellen.
-- Plak dit in de SQL Editor van Supabase en draai het één keer.

create table if not exists whoop_days (
  id              uuid primary key default gen_random_uuid(),
  user_id         uuid not null references auth.users(id) on delete cascade,
  day             date not null,              -- dag waarop je wakker werd

  -- hartslag
  hr_avg          real,
  hr_min          real,
  hr_max          real,
  rhr             real,                       -- laagste 60s-gemiddelde
  worn_min        integer,                    -- minuten met een hartslag
  battery         real,                       -- accustand van de band
  resp_rate       real,                       -- ademhaling (nog niet gedecodeerd)
  spo2            real,                       -- zuurstof: niet haalbaar, kolom blijft leeg
  stress_rmssd    real,                       -- HRV in rust overdag; lager = meer activatie
  gevoel          smallint,                   -- eigen ochtendcijfer 1-5
  skin_temp       real,                       -- huidtemperatuur, afwijking t.o.v. baseline

  -- HRV
  hrv_rmssd       real,
  hrv_sdnn        real,
  ln_rmssd        real,
  hrv_n           integer,

  -- slaap
  sleep_start     timestamptz,
  sleep_end       timestamptz,
  sleep_min       integer,
  sleep_waso_min  integer,
  sleep_efficiency real,

  -- belasting
  trimp_edwards   real,
  trimp_banister  real,
  strain21        real,

  -- herstel (null tot de baseline vol is)
  recovery        real,
  recovery_z      real,
  baseline_days   integer,

  -- voor de grafiek op de telefoon: ~120 gedownsamplede punten
  hr_curve        jsonb,
  zones           jsonb,

  updated_at      timestamptz not null default now(),
  unique (user_id, day)
);

alter table whoop_days enable row level security;

drop policy if exists "eigen whoop-rijen" on whoop_days;
create policy "eigen whoop-rijen" on whoop_days
  for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

create index if not exists whoop_days_user_day_idx
  on whoop_days (user_id, day desc);
