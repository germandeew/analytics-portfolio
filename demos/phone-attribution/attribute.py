#!/usr/bin/env python3
"""Атрибуция регистраций и оплат по каналам и проверки качества перед выпуском отчёта.

Правило атрибуции записано в одном месте (ATTRIBUTION_SQL):
  1. метка рекламы или партнёра у регистрации главнее всего — это чужой канал;
  2. иначе — последнее касание наших каналов по тому же телефону за WINDOW_DAYS дней до регистрации;
  3. иначе — органика.
Оплата наследует канал регистрации.

Выпуск отчёта останавливается (HOLD), если красная хоть одна проверка: доля мусорных телефонов,
оплаты без регистрации, расхождение сверки, «молчащий» день источника регистраций.
Только стандартная библиотека Python 3.9+.
"""
import argparse
import csv
import pathlib
import re
import sqlite3
import sys

WINDOW_DAYS = 10
MAX_BAD_PHONE_SHARE = 0.03
SILENT_DAY_MIN_AVG = 10  # ноль регистраций при среднем ≥ 10 за 7 дней до этого — источник молчит

ATTRIBUTION_SQL = f"""
create table attributed as
with last_touch as (
  select r.user_id, t.channel,
         row_number() over (partition by r.user_id order by datetime(t.touched_at) desc) as rn
  from registrations r
  join touches t
    on t.phone = r.phone
   and datetime(t.touched_at) <= datetime(r.registered_at)
   and datetime(t.touched_at) >= datetime(r.registered_at, '-{int(WINDOW_DAYS)} days')
  where r.phone is not null and r.marker = ''
)
select r.user_id,
       case when r.marker <> '' then r.marker
            when lt.channel is not null then lt.channel
            else 'organic' end as channel,
       case when r.marker = '' and lt.channel is not null then 'наши' else 'другие' end as source_group
from registrations r
left join last_touch lt on lt.user_id = r.user_id and lt.rn = 1
"""


def normalize_phone(raw):
    """Российский мобильный номер в виде 79XXXXXXXXX или None, если номер не распознан."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10 and digits.startswith("9"):
        digits = "7" + digits
    elif len(digits) == 11 and digits[0] in "78":
        digits = "7" + digits[1:]
    return digits if len(digits) == 11 and digits.startswith("79") else None


def create_tables(con):
    con.executescript("""
        create table touches (channel text not null, phone text, phone_raw text, touched_at text not null);
        create table registrations (user_id integer primary key, phone text, phone_raw text,
                                    registered_at text not null, marker text not null default '');
        create table payments (user_id integer not null, paid_at text not null, amount real not null);
    """)


def add_touch(con, channel, phone_raw, touched_at):
    con.execute("insert into touches values (?, ?, ?, ?)",
                (channel, normalize_phone(phone_raw), phone_raw, touched_at))


def add_registration(con, user_id, phone_raw, registered_at, marker=""):
    con.execute("insert into registrations values (?, ?, ?, ?, ?)",
                (user_id, normalize_phone(phone_raw), phone_raw, registered_at, marker or ""))


def add_payment(con, user_id, paid_at, amount):
    con.execute("insert into payments values (?, ?, ?)", (user_id, paid_at, amount))


def load_csv(con, data_dir):
    def rows(name):
        with open(data_dir / name, newline="", encoding="utf-8") as f:
            yield from csv.DictReader(f)

    for r in rows("touches.csv"):
        add_touch(con, r["channel"], r["phone"], r["touched_at"])
    for r in rows("registrations.csv"):
        add_registration(con, int(r["user_id"]), r["phone"], r["registered_at"], r["marker"])
    for r in rows("payments.csv"):
        add_payment(con, int(r["user_id"]), r["paid_at"], float(r["amount"]))


def attribute(con):
    con.execute("drop table if exists attributed")
    con.execute(ATTRIBUTION_SQL)


def channel_report(con):
    sql = """
        with pay as (select user_id, sum(amount) as revenue from payments group by user_id)
        select a.channel, a.source_group, count(*) as regs, count(pay.user_id) as payers,
               coalesce(sum(pay.revenue), 0) as revenue
        from attributed a left join pay on pay.user_id = a.user_id
        group by a.channel, a.source_group
        order by a.source_group = 'наши' desc, revenue desc
    """
    rows = [dict(zip(("channel", "group", "regs", "payers", "revenue"), r)) for r in con.execute(sql)]
    total = sum(r["regs"] for r in rows) or 1
    for r in rows:
        r["share"] = r["regs"] / total
        r["revenue_per_reg"] = r["revenue"] / r["regs"]
    return rows


def run_checks(con, report):
    """Список (проверка, зелёная ли, подробность). Хоть одна красная — выпуск HOLD."""
    checks = []

    bad, total = con.execute("""
        select sum(phone is null), count(*) from (
          select phone from touches union all select phone from registrations)
    """).fetchone()
    share = (bad or 0) / (total or 1)
    checks.append(("Мусорные телефоны", share <= MAX_BAD_PHONE_SHARE,
                   f"{share:.1%} при пороге {MAX_BAD_PHONE_SHARE:.0%}"))

    orphans = con.execute("""
        select count(*) from payments p
        where not exists (select 1 from registrations r where r.user_id = p.user_id)
    """).fetchone()[0]
    checks.append(("Оплаты без регистрации", orphans == 0, str(orphans)))

    paid = con.execute("select coalesce(sum(amount), 0) from payments").fetchone()[0]
    diff = paid - sum(r["revenue"] for r in report)
    checks.append(("Сверка выручки с оплатами", abs(diff) < 0.005, f"расхождение {diff:,.2f} ₽"))

    silent = con.execute("""
        with recursive bounds as (
          select min(d) as lo, max(d) as hi from (
            select date(registered_at) as d from registrations
            union all select date(touched_at) from touches
            union all select date(paid_at) from payments)),
        days(d) as (
          select lo from bounds
          union all select date(d, '+1 day') from days, bounds where d < hi),
        daily as (
          select days.d, count(r.user_id) as n
          from days left join registrations r on date(r.registered_at) = days.d
          group by days.d),
        w as (
          select d, n, avg(n) over (order by d rows between 7 preceding and 1 preceding) as prev_avg
          from daily)
        select d, prev_avg from w where n = 0 and prev_avg >= ?
    """, (SILENT_DAY_MIN_AVG,)).fetchall()
    checks.append(("Источник регистраций не молчит", not silent,
                   ", ".join(f"{d}: 0 при среднем {avg:.1f}" for d, avg in silent) or "пропусков нет"))
    return checks


def render(report, checks):
    status = "OK" if all(ok for _, ok, _ in checks) else "HOLD"
    lines = [f"# Атрибуция регистраций и оплат — выпуск: {status}", "",
             "| Канал | Группа | Регистраций | Доля | Платящих | Выручка, ₽ | ₽ на регистрацию |",
             "|---|---|---:|---:|---:|---:|---:|"]
    for r in report:
        lines.append(f"| {r['channel']} | {r['group']} | {r['regs']} | {r['share']:.1%} | {r['payers']} "
                     f"| {r['revenue']:,.0f} | {r['revenue_per_reg']:,.0f} |".replace(",", " "))
    lines += ["", "## Проверки", ""]
    lines += [f"- {'OK  ' if ok else 'HOLD'} {name}: {detail}" for name, ok, detail in checks]
    return status, "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", default="data", type=pathlib.Path)
    ap.add_argument("--out", default="report.md", type=pathlib.Path)
    args = ap.parse_args(argv)

    con = sqlite3.connect(":memory:")
    create_tables(con)
    load_csv(con, args.data)
    attribute(con)
    report = channel_report(con)
    checks = run_checks(con, report)
    status, text = render(report, checks)
    args.out.write_text(text, encoding="utf-8")
    print(text)
    return 0 if status == "OK" else 2


if __name__ == "__main__":
    sys.exit(main())
