#!/usr/bin/env python3
"""Выдуманные данные для демо атрибуции: касания наших каналов, регистрации, оплаты.

Телефоны нарочно записаны по-разному и иногда с мусором, как в живых выгрузках.
Флаг --silent-day оставляет последний день без регистраций (касания при этом идут),
чтобы показать, как проверка останавливает выпуск отчёта.
"""
import argparse
import csv
import datetime as dt
import pathlib
import random

CHANNELS = ["sms", "call", "messenger"]
MARKERS = ["ads", "partner"]


def fmt_phone(rnd, d):
    """Один и тот же номер в разных записях выглядит по-разному."""
    return rnd.choice([f"+7 ({d[:3]}) {d[3:6]}-{d[6:8]}-{d[8:]}", f"8 {d[:3]} {d[3:6]} {d[6:8]} {d[8:]}",
                       d, f"7{d}"])


def write(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="data", type=pathlib.Path)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--silent-day", action="store_true", help="последний день без регистраций")
    a = ap.parse_args()

    rnd = random.Random(a.seed)
    start = dt.datetime(2026, 9, 1)
    end = start + dt.timedelta(days=a.days) - dt.timedelta(seconds=1)
    touches, regs, pays = [], [], []
    user_id = 0
    for day in range(a.days):
        silent = a.silent_day and day == a.days - 1
        for _ in range(rnd.randint(25, 40)):
            phone = "9" + "".join(rnd.choice("0123456789") for _ in range(9))
            at = start + dt.timedelta(days=day, minutes=rnd.randrange(24 * 60))
            if rnd.random() < 0.45:  # человека касались наши каналы, иногда давно
                for _ in range(rnd.randint(1, 3)):
                    back = dt.timedelta(days=rnd.uniform(0, 20)) if not silent else dt.timedelta(0)
                    touches.append({"channel": rnd.choice(CHANNELS), "phone": fmt_phone(rnd, phone),
                                    "touched_at": (at - back).isoformat(timespec="seconds")})
            if silent:
                continue
            user_id += 1
            regs.append({"user_id": user_id,
                         "phone": "нет номера" if rnd.random() < 0.02 else fmt_phone(rnd, phone),
                         "registered_at": at.isoformat(timespec="seconds"),
                         "marker": rnd.choice(MARKERS) if rnd.random() < 0.3 else ""})
            if rnd.random() < 0.2:
                paid = min(at + dt.timedelta(days=rnd.uniform(0, 5)), end)
                pays.append({"user_id": user_id, "paid_at": paid.isoformat(timespec="seconds"),
                             "amount": rnd.choice([600, 1500, 2700, 4200])})

    a.out.mkdir(parents=True, exist_ok=True)
    write(a.out / "touches.csv", touches, ["channel", "phone", "touched_at"])
    write(a.out / "registrations.csv", regs, ["user_id", "phone", "registered_at", "marker"])
    write(a.out / "payments.csv", pays, ["user_id", "paid_at", "amount"])
    print(f"касаний {len(touches)}, регистраций {len(regs)}, оплат {len(pays)} → {a.out}/")


if __name__ == "__main__":
    main()
