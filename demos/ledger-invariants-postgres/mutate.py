#!/usr/bin/env python3
"""Мутационное тестирование правил базы.

По очереди портим одно правило в schema.sql, поднимаем чистую базу и гоняем тесты.
Тесты упали: мутант «убит», правило под охраной. Тесты прошли: мутант «выжил», правило
можно сломать незаметно, нужен тест. Подключение берётся из PGHOST, PGPORT, PGUSER.
"""
import os
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
DB = os.environ.get("MUTATION_DB", "ledger_demo_mutant")
if not DB.startswith("ledger_demo"):
    sys.exit("имя базы должно начинаться с ledger_demo: скрипт её пересоздаёт")

# (что ломаем, исходный фрагмент schema.sql, замена). Фрагмент обязан встречаться ровно один раз,
# иначе после правки схемы мутация молча перестала бы что-либо портить.
MUTATIONS = [
    ("баланс проводки: ловим только перекос в плюс",
     "if v_sum <> 0 then", "if v_sum > 0 then"),
    ("строка с нулевой суммой разрешена",
     "check (amount <> 0)", "check (true)"),
    ("перевод: разрешена нулевая сумма",
     "if p_amount is null or p_amount <= 0 then", "if p_amount is null or p_amount < 0 then"),
    ("перевод на тот же счёт разрешён",
     "if p_from = p_to then", "if false then"),
    ("перевод между разными валютами разрешён",
     "if v_from.currency <> v_to.currency then", "if false then"),
    ("остаток сравнивается с нулём, а не с суммой",
     "if v_balance < p_amount then", "if v_balance < 0 then"),
    ("проверка остатка выключена",
     "if not v_from.overdraft_allowed then", "if false then"),
    ("остаток проверяется и у касс",
     "if not v_from.overdraft_allowed then", "if true then"),
    ("перевод: обе строки с плюсом",
     "values (v_entry, p_from, -p_amount), (v_entry, p_to, p_amount)",
     "values (v_entry, p_from, p_amount), (v_entry, p_to, p_amount)"),
    ("сторно повторяет знаки вместо обратных",
     "select v_entry, l.account_id, -l.amount", "select v_entry, l.account_id, l.amount"),
    ("одну проводку можно сторнировать дважды",
     "reverses_entry_id bigint unique references acct.entry",
     "reverses_entry_id bigint references acct.entry"),
    ("проводка без строк разрешена",
     "if not exists (select 1 from acct.line where entry_id = new.entry_id) then", "if false then"),
    ("строки проводок можно менять",
     "before update or delete on acct.line", "before delete on acct.line"),
    ("проводки можно удалять",
     "before update or delete on acct.entry", "before update on acct.entry"),
    ("клиент видит все счета",
     "  using (party_id = acct.current_party());", "  using (true);"),
    ("представление остатков обходит изоляцию строк",
     "with (security_invoker = true)", "with (security_invoker = false)"),
    ("перевод доступен всем ролям",
     "revoke execute on function acct.post_transfer(bigint, bigint, numeric, text) from public;", ""),
    ("сторно доступно всем ролям",
     "revoke execute on function acct.reverse_entry(bigint, text) from public;", ""),
    # Четвёртое поле: мутация осмысленна только на версиях ниже указанной. С PostgreSQL 18
    # отложенный триггер выполняется с ролью, которая поставила событие в очередь (здесь это
    # владелец команды), поэтому без security definer ничего не ломается: мутант эквивалентен.
    # На 15–17 триггер выполняется с ролью на момент commit, и тест 20 должен его убить.
    ("проверка баланса работает с правами того, кто делает commit",
     "check_entry_balanced() returns trigger\nlanguage plpgsql security definer",
     "check_entry_balanced() returns trigger\nlanguage plpgsql", 180000),
]


def psql(db, *args):
    cmd = ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", db, *args]
    return subprocess.run(cmd, capture_output=True, text=True)


def run_suite(schema_sql):
    for sql in (f'drop database if exists "{DB}"', f'create database "{DB}"'):
        r = psql("postgres", "-c", sql)
        if r.returncode:
            sys.exit(f"не удалось пересоздать базу {DB}: {r.stderr.strip()}")
    mutant = HERE / ".mutant.sql"
    mutant.write_text(schema_sql, encoding="utf-8")
    try:
        r = psql(DB, "-f", str(mutant), "-f", str(HERE / "seed.sql"), "-f", str(HERE / "tests.sql"))
    finally:
        mutant.unlink()
    last = (r.stderr.strip().splitlines() or [""])[-1]
    return r.returncode == 0, last


def main():
    schema = (HERE / "schema.sql").read_text(encoding="utf-8")
    version = int(psql("postgres", "-At", "-c", "show server_version_num").stdout.strip())
    ok, why = run_suite(schema)
    if not ok:
        sys.exit(f"тесты красные и без мутаций: {why}")

    survived, skipped = [], 0
    for i, (name, old, new, *rest) in enumerate(MUTATIONS, 1):
        n = schema.count(old)
        if n != 1:
            sys.exit(f"мутация {i} «{name}»: фрагмент встречается {n} раз, а должен ровно один")
        if rest and version >= rest[0]:
            print(f"{i:2}. пропуск {name} (эквивалентна на PostgreSQL {version // 10000})")
            skipped += 1
            continue
        passed, why = run_suite(schema.replace(old, new))
        print(f"{i:2}. {'ВЫЖИЛ' if passed else 'убит '}  {name}")
        if passed:
            survived.append(name)

    psql("postgres", "-c", f'drop database if exists "{DB}"')
    tried = len(MUTATIONS) - skipped
    print(f"\nУбито {tried - len(survived)} из {tried}" + (f", пропущено {skipped}." if skipped else "."))
    sys.exit(1 if survived else 0)


if __name__ == "__main__":
    main()
