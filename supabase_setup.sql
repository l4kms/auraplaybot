-- ══════════════════════════════════════════════════════════════
--  AURA Bot — SQL для Supabase
--  Запусти в Supabase → SQL Editor → New query
-- ══════════════════════════════════════════════════════════════


-- 1. ТАБЛИЦА НАСТРОЕК САЙТА
--    Хранит флаг blocked, настройки пароля и т.д.
-- ──────────────────────────────────────────────
create table if not exists settings (
  id          int primary key default 1,
  blocked     boolean not null default false,
  pw_enabled  boolean not null default true,
  pw_hash     text,
  updated_at  timestamptz not null default now(),
  constraint single_row check (id = 1)
);

-- Вставляем начальную строку если её нет
insert into settings (id, blocked, pw_enabled)
values (1, false, true)
on conflict (id) do nothing;

-- RLS
alter table settings enable row level security;
drop policy if exists "allow_all" on settings;
create policy "allow_all" on settings
  for all using (true) with check (true);


-- 2. ТРИГГЕР — уведомление при добавлении трека
--    Требует расширение pg_net (уже есть в Supabase)
-- ──────────────────────────────────────────────
-- Замени URL и секрет на свои!
create or replace function notify_track_added()
returns trigger
language plpgsql
as $$
declare
  bot_url  text := 'https://ТВОЙ-ДОМЕН.railway.app/webhook/track-added';
  secret   text := 'aura_secret_2024';
begin
  perform net.http_post(
    url     := bot_url,
    body    := json_build_object(
                 'id',     NEW.id,
                 'title',  NEW.title,
                 'artist', NEW.artist
               )::text,
    headers := json_build_object(
                 'Content-Type',      'application/json',
                 'x-webhook-secret',  secret
               )::jsonb
  );
  return NEW;
end;
$$;

-- Удаляем старый триггер если есть
drop trigger if exists trg_track_added on tracks;

-- Создаём триггер
create trigger trg_track_added
  after insert on tracks
  for each row
  execute procedure notify_track_added();


-- ══════════════════════════════════════════════════════════════
--  ГОТОВО. После деплоя бота:
--  1. Замени 'https://ТВОЙ-ДОМЕН...' на реальный URL
--  2. Перезапусти SQL (или обнови функцию через ALTER)
-- ══════════════════════════════════════════════════════════════
