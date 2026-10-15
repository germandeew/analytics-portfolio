-- Тесты правил учёта. Каждый тест в своей транзакции с откатом, поэтому порядок не важен.
-- Стартовые остатки из seed.sql: Альфа основной 1000, Альфа резерв 0, Бета основной 500,
-- Бета валютный 100 USD, Касса −1500, Касса USD −100.
\set ON_ERROR_STOP on
-- результаты запросов не нужны, видны только \echo и ошибки
\o /dev/null

create schema test;
grant usage on schema test to demo_app, demo_client;

-- id счёта по названию; security definer, чтобы помощник работал и под ролью клиента
create function test.acc(p_title text) returns bigint
language sql stable security definer set search_path = acct, pg_temp as $$
  select account_id from acct.account where title = p_title
$$;

create function test.bal(p_title text) returns numeric
language sql stable security definer set search_path = acct, pg_temp as $$
  select coalesce(sum(l.amount), 0)
  from acct.line l join acct.account a on a.account_id = l.account_id
  where a.title = p_title
$$;

-- Ждём ошибку: по коду SQLSTATE или по шаблону LIKE в тексте. Нет ошибки — тест упал.
create function test.expect_error(p_sql text, p_expect text) returns void
language plpgsql as $$
begin
  begin
    execute p_sql;
  exception when others then
    if sqlstate = p_expect or sqlerrm like p_expect then
      return;
    end if;
    raise exception 'ожидали «%», получили «%» (%)', p_expect, sqlerrm, sqlstate;
  end;
  raise exception 'ожидали ошибку «%», но запрос прошёл: %', p_expect, p_sql;
end $$;

\echo ' 1. перевод меняет остатки, сумма по валюте остаётся нулевой'
begin;
do $$
begin
  perform acct.post_transfer(test.acc('Альфа: основной'), test.acc('Бета: основной'), 300, 'оплата услуг');
  assert test.bal('Альфа: основной') = 700, 'у Альфы должно стать 700';
  assert test.bal('Бета: основной') = 800, 'у Беты должно стать 800';
  assert (select sum(l.amount) from acct.line l join acct.account a on a.account_id = l.account_id
          where a.currency = 'RUB') = 0, 'сумма всех рублёвых строк должна быть 0';
end $$;
set constraints all immediate;
rollback;

\echo ' 2. нулевая сумма отклонена'
select test.expect_error(format('select acct.post_transfer(%s, %s, 0, %L)',
  test.acc('Альфа: основной'), test.acc('Бета: основной'), 'ноль'), '%больше нуля%');

\echo ' 3. отрицательная сумма отклонена'
select test.expect_error(format('select acct.post_transfer(%s, %s, -5, %L)',
  test.acc('Альфа: основной'), test.acc('Бета: основной'), 'минус'), '%больше нуля%');

\echo ' 4. перевод на тот же счёт отклонён'
select test.expect_error(format('select acct.post_transfer(%s, %s, 50, %L)',
  test.acc('Альфа: основной'), test.acc('Альфа: основной'), 'сам себе'), '%тот же счёт%');

\echo ' 5. перевод между валютами отклонён'
select test.expect_error(format('select acct.post_transfer(%s, %s, 10, %L)',
  test.acc('Бета: основной'), test.acc('Бета: валютный'), 'рубли в доллары'), '%валюты%');

\echo ' 6. нельзя потратить больше остатка'
select test.expect_error(format('select acct.post_transfer(%s, %s, 1, %L)',
  test.acc('Альфа: резерв'), test.acc('Бета: основной'), 'с пустого счёта'), '%недостаточно средств%');

\echo ' 7. системная касса может уходить в минус'
begin;
do $$
begin
  perform acct.post_transfer(test.acc('Касса'), test.acc('Альфа: резерв'), 50, 'выдача из кассы');
  assert test.bal('Касса') = -1550, 'касса должна стать −1550';
end $$;
set constraints all immediate;
rollback;

\echo ' 8. несбалансированная проводка (перекос в минус) отклонена'
begin;
do $$
declare v_e bigint;
begin
  insert into acct.entry (memo) values ('кривая проводка') returning entry_id into v_e;
  insert into acct.line (entry_id, account_id, amount)
  values (v_e, test.acc('Альфа: основной'), -100), (v_e, test.acc('Бета: основной'), 90);
  perform test.expect_error('set constraints all immediate', '%не сбалансирована%');
end $$;
rollback;

\echo ' 9. несбалансированная проводка (перекос в плюс) отклонена'
begin;
do $$
declare v_e bigint;
begin
  insert into acct.entry (memo) values ('кривая проводка') returning entry_id into v_e;
  insert into acct.line (entry_id, account_id, amount)
  values (v_e, test.acc('Альфа: основной'), 100), (v_e, test.acc('Бета: основной'), -90);
  perform test.expect_error('set constraints all immediate', '%не сбалансирована%');
end $$;
rollback;

\echo '10. строка с нулевой суммой отклонена'
begin;
do $$
declare v_e bigint;
begin
  insert into acct.entry (memo) values ('проводка с пустой строкой') returning entry_id into v_e;
  insert into acct.line (entry_id, account_id, amount)
  values (v_e, test.acc('Альфа: основной'), -100), (v_e, test.acc('Бета: основной'), 100);
  perform test.expect_error(format('insert into acct.line (entry_id, account_id, amount) values (%s, %s, 0)',
    v_e, test.acc('Альфа: резерв')), '%line_amount_check%');
end $$;
rollback;

\echo '11. проводка без строк отклонена'
begin;
do $$
begin
  insert into acct.entry (memo) values ('пустая проводка');
  perform test.expect_error('set constraints all immediate', '%без строк%');
end $$;
rollback;

\echo '12. строку проводки нельзя изменить'
select test.expect_error('update acct.line set amount = amount where line_id = (select min(line_id) from acct.line)',
  '%только для добавления%');

\echo '13. проводку нельзя удалить'
select test.expect_error('delete from acct.entry where entry_id = (select min(entry_id) from acct.entry)',
  '%только для добавления%');

\echo '14. таблицу строк нельзя очистить'
select test.expect_error('truncate acct.line', '%только для добавления%');

\echo '15. сторно возвращает остатки, исходная проводка остаётся в истории'
begin;
do $$
declare v_e bigint;
begin
  v_e := acct.post_transfer(test.acc('Альфа: основной'), test.acc('Бета: основной'), 250, 'ошибочный платёж');
  perform acct.reverse_entry(v_e, 'сторно ошибочного платежа');
  assert test.bal('Альфа: основной') = 1000, 'после сторно у Альфы снова 1000';
  assert test.bal('Бета: основной') = 500, 'после сторно у Беты снова 500';
  assert exists (select 1 from acct.entry where entry_id = v_e), 'исходная проводка на месте';
end $$;
set constraints all immediate;
rollback;

\echo '16. повторное сторно той же проводки отклонено'
begin;
do $$
declare v_e bigint;
begin
  v_e := acct.post_transfer(test.acc('Альфа: основной'), test.acc('Бета: основной'), 10, 'платёж');
  perform acct.reverse_entry(v_e, 'сторно');
  perform test.expect_error(format('select acct.reverse_entry(%s, %L)', v_e, 'второе сторно'), '23505');
end $$;
rollback;

\echo '17. клиент видит только свои счета и остатки'
begin;
select set_config('app.party_id', (select party_id::text from acct.party where name = 'ООО Альфа'), true);
set local role demo_client;
do $$
begin
  assert (select count(*) from acct.account) = 2, 'у Альфы два счёта';
  assert (select count(*) from acct.balance) = 2, 'в остатках тоже только два счёта Альфы';
  assert (select sum(balance) from acct.balance) = 1000, 'сумма остатков Альфы 1000';
end $$;
rollback;

\echo '18. клиент без стороны не видит ничего'
begin;
set local role demo_client;
do $$
begin
  assert (select count(*) from acct.account) = 0, 'без app.party_id счетов не видно';
  assert (select count(*) from acct.line) = 0, 'без app.party_id строк не видно';
end $$;
rollback;

\echo '19. клиент не может провести перевод или сторно'
begin;
select set_config('app.party_id', (select party_id::text from acct.party where name = 'ООО Альфа'), true);
set local role demo_client;
do $$
begin
  perform test.expect_error(format('select acct.post_transfer(%s, %s, 1, %L)',
    test.acc('Альфа: основной'), test.acc('Бета: основной'), 'попытка клиента'), '42501');
  perform test.expect_error('select acct.reverse_entry(1, ''попытка клиента'')', '42501');
end $$;
rollback;

\echo '20. приложение пишет только через команды'
begin;
set local role demo_app;
do $$
begin
  perform test.expect_error('insert into acct.entry (memo) values (''напрямую'')', '42501');
  perform acct.post_transfer(test.acc('Бета: основной'), test.acc('Альфа: резерв'), 20, 'через команду');
  assert test.bal('Альфа: резерв') = 20, 'перевод через команду прошёл';
end $$;
set constraints all immediate;
rollback;

\echo 'все тесты прошли'
