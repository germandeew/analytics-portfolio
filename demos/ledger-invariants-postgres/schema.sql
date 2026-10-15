-- Учёт денег по счетам: проводки, сторно, правила внутри базы, изоляция строк.
-- Демо на выдуманных данных, PostgreSQL 15+. Запуск и тесты описаны в README.md.

create schema acct;

-- Роли демо. Роли общие для всего кластера, поэтому создаём, только если их нет.
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'demo_app') then
    create role demo_app nologin;      -- приложение: пишет только через команды
  end if;
  if not exists (select 1 from pg_roles where rolname = 'demo_client') then
    create role demo_client nologin;   -- клиент: читает только своё
  end if;
end $$;

create table acct.party (
  party_id bigint generated always as identity primary key,
  name     text not null unique
);

create table acct.account (
  account_id        bigint generated always as identity primary key,
  party_id          bigint references acct.party,   -- null: системный счёт (касса)
  currency          char(3) not null check (currency ~ '^[A-Z]{3}$'),
  title             text not null unique,
  overdraft_allowed boolean not null default false
);

create table acct.entry (
  entry_id          bigint generated always as identity primary key,
  occurred_at       timestamptz not null default now(),
  memo              text not null,
  reverses_entry_id bigint unique references acct.entry  -- у проводки не больше одного сторно
);

create table acct.line (
  line_id    bigint generated always as identity primary key,
  entry_id   bigint not null references acct.entry,
  account_id bigint not null references acct.account,
  amount     numeric(14, 2) not null check (amount <> 0)
);
create index line_account_idx on acct.line (account_id);
create index line_entry_idx on acct.line (entry_id);

-- Правило 1. Проводка сбалансирована: сумма её строк равна нулю.
-- Проверяется в конце транзакции, когда записаны все строки. Отложенный триггер срабатывает
-- с правами того, кто делает commit (например, роли приложения), поэтому security definer.
create function acct.check_entry_balanced() returns trigger
language plpgsql security definer set search_path = acct, pg_temp as $$
declare
  v_sum numeric;
begin
  select coalesce(sum(amount), 0) into v_sum from acct.line where entry_id = new.entry_id;
  if v_sum <> 0 then
    raise exception 'проводка % не сбалансирована: сумма строк %', new.entry_id, v_sum
      using errcode = 'check_violation';
  end if;
  return null;
end $$;

create constraint trigger line_balanced
  after insert on acct.line
  deferrable initially deferred
  for each row execute function acct.check_entry_balanced();

-- Правило 2. Проводки без строк не бывает.
create function acct.check_entry_has_lines() returns trigger
language plpgsql security definer set search_path = acct, pg_temp as $$
begin
  if not exists (select 1 from acct.line where entry_id = new.entry_id) then
    raise exception 'проводка % без строк', new.entry_id using errcode = 'check_violation';
  end if;
  return null;
end $$;

create constraint trigger entry_has_lines
  after insert on acct.entry
  deferrable initially deferred
  for each row execute function acct.check_entry_has_lines();

-- Правило 3. История не переписывается: ошибку исправляет сторно, а не update или delete.
create function acct.forbid_change() returns trigger
language plpgsql as $$
begin
  raise exception 'таблица % только для добавления: ошибку исправляет сторно', tg_table_name
    using errcode = 'prohibited_sql_statement_attempted';
end $$;

create trigger entry_append_only before update or delete on acct.entry
  for each row execute function acct.forbid_change();
create trigger line_append_only before update or delete on acct.line
  for each row execute function acct.forbid_change();
create trigger entry_no_truncate before truncate on acct.entry
  for each statement execute function acct.forbid_change();
create trigger line_no_truncate before truncate on acct.line
  for each statement execute function acct.forbid_change();

-- Остатки считаются из строк и отдельно не хранятся: расхождению негде появиться.
create view acct.balance with (security_invoker = true) as
select a.account_id, a.party_id, a.currency, a.title,
       coalesce(sum(l.amount), 0)::numeric(14, 2) as balance
from acct.account a
left join acct.line l on l.account_id = a.account_id
group by a.account_id;

-- Команда «перевод». Для приложения это единственный способ записать деньги.
create function acct.post_transfer(p_from bigint, p_to bigint, p_amount numeric, p_memo text)
returns bigint
language plpgsql
security definer
set search_path = acct, pg_temp
as $$
declare
  v_from    acct.account;
  v_to      acct.account;
  v_balance numeric;
  v_entry   bigint;
begin
  if p_amount is null or p_amount <= 0 then
    raise exception 'сумма перевода должна быть больше нуля, получено %', p_amount
      using errcode = 'check_violation';
  end if;
  if p_from = p_to then
    raise exception 'перевод на тот же счёт' using errcode = 'check_violation';
  end if;

  -- Блокируем оба счёта в одном порядке: параллельные переводы не обойдут проверку остатка
  -- и не устроят взаимную блокировку.
  perform 1 from acct.account where account_id in (p_from, p_to) order by account_id for update;
  select * into v_from from acct.account where account_id = p_from;
  select * into v_to from acct.account where account_id = p_to;
  if v_from.account_id is null or v_to.account_id is null then
    raise exception 'счёт не найден' using errcode = 'foreign_key_violation';
  end if;
  if v_from.currency <> v_to.currency then
    raise exception 'валюты счетов различаются: % и %', v_from.currency, v_to.currency
      using errcode = 'check_violation';
  end if;

  if not v_from.overdraft_allowed then
    select coalesce(sum(amount), 0) into v_balance from acct.line where account_id = p_from;
    if v_balance < p_amount then
      raise exception 'недостаточно средств на счёте %: остаток %, нужно %', p_from, v_balance, p_amount
        using errcode = 'check_violation';
    end if;
  end if;

  insert into acct.entry (memo) values (p_memo) returning entry_id into v_entry;
  insert into acct.line (entry_id, account_id, amount)
  values (v_entry, p_from, -p_amount), (v_entry, p_to, p_amount);
  return v_entry;
end $$;

-- Команда «сторно»: новая проводка с обратными знаками, исходная остаётся в истории.
-- Остаток не проверяется: это исправление ошибки, а не новый платёж.
create function acct.reverse_entry(p_entry_id bigint, p_memo text)
returns bigint
language plpgsql
security definer
set search_path = acct, pg_temp
as $$
declare
  v_entry bigint;
begin
  if not exists (select 1 from acct.entry where entry_id = p_entry_id) then
    raise exception 'проводка % не найдена', p_entry_id using errcode = 'foreign_key_violation';
  end if;
  insert into acct.entry (memo, reverses_entry_id) values (p_memo, p_entry_id)
  returning entry_id into v_entry;
  insert into acct.line (entry_id, account_id, amount)
  select v_entry, l.account_id, -l.amount from acct.line l where l.entry_id = p_entry_id;
  return v_entry;
end $$;

-- Изоляция строк: клиент видит только счета своей стороны.
create function acct.current_party() returns bigint
language sql stable as $$
  select nullif(current_setting('app.party_id', true), '')::bigint
$$;

alter table acct.account enable row level security;
alter table acct.line enable row level security;

create policy account_own on acct.account for select to demo_client
  using (party_id = acct.current_party());
create policy line_own on acct.line for select to demo_client
  using (exists (select 1 from acct.account a
                 where a.account_id = line.account_id and a.party_id = acct.current_party()));

-- Права. Функцию по умолчанию может вызвать любой (PUBLIC), поэтому команды закрываем явно.
revoke execute on function acct.post_transfer(bigint, bigint, numeric, text) from public;
revoke execute on function acct.reverse_entry(bigint, text) from public;
grant usage on schema acct to demo_app, demo_client;
grant execute on function acct.post_transfer(bigint, bigint, numeric, text) to demo_app;
grant execute on function acct.reverse_entry(bigint, text) to demo_app;
grant select on acct.account, acct.line, acct.balance to demo_client;
