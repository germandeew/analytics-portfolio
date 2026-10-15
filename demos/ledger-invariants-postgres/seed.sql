-- Выдуманные данные: две стороны, две системные кассы и стартовые остатки.
-- Остатки заводятся той же командой, что и рабочие переводы.

insert into acct.party (name) values ('ООО Альфа'), ('ИП Бета');

insert into acct.account (party_id, currency, title, overdraft_allowed) values
  (null, 'RUB', 'Касса', true),
  (null, 'USD', 'Касса USD', true),
  ((select party_id from acct.party where name = 'ООО Альфа'), 'RUB', 'Альфа: основной', false),
  ((select party_id from acct.party where name = 'ООО Альфа'), 'RUB', 'Альфа: резерв', false),
  ((select party_id from acct.party where name = 'ИП Бета'), 'RUB', 'Бета: основной', false),
  ((select party_id from acct.party where name = 'ИП Бета'), 'USD', 'Бета: валютный', false);

do $$
begin
  perform acct.post_transfer((select account_id from acct.account where title = 'Касса'),
                             (select account_id from acct.account where title = 'Альфа: основной'),
                             1000, 'стартовый остаток');
  perform acct.post_transfer((select account_id from acct.account where title = 'Касса'),
                             (select account_id from acct.account where title = 'Бета: основной'),
                             500, 'стартовый остаток');
  perform acct.post_transfer((select account_id from acct.account where title = 'Касса USD'),
                             (select account_id from acct.account where title = 'Бета: валютный'),
                             100, 'стартовый остаток');
end $$;
