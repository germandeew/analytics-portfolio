"""Тесты правила атрибуции и проверок выпуска. Запуск: python3 -m unittest -v"""
import sqlite3
import unittest

import attribute as A


class Base(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        A.create_tables(self.con)

    def run_all(self):
        A.attribute(self.con)
        report = A.channel_report(self.con)
        return report, {name: ok for name, ok, _ in A.run_checks(self.con, report)}

    def channel_of(self, user_id):
        return self.con.execute("select channel from attributed where user_id = ?", (user_id,)).fetchone()[0]


class NormalizePhone(unittest.TestCase):
    def test_formats(self):
        for raw in ["+7 (912) 345-67-89", "8 912 345 67 89", "9123456789", "79123456789", "8-912-345-67-89"]:
            self.assertEqual(A.normalize_phone(raw), "79123456789", raw)

    def test_garbage(self):
        for raw in ["", None, "нет номера", "12345", "+7 (343) 123-45-67", "891234567890"]:
            self.assertIsNone(A.normalize_phone(raw), raw)


class Attribution(Base):
    def test_marker_beats_touch(self):
        A.add_touch(self.con, "sms", "+7 912 000-00-01", "2026-09-10T10:00:00")
        A.add_registration(self.con, 1, "89120000001", "2026-09-10T12:00:00", marker="ads")
        self.run_all()
        self.assertEqual(self.channel_of(1), "ads")

    def test_last_touch_wins(self):
        A.add_touch(self.con, "sms", "9120000002", "2026-09-05T10:00:00")
        A.add_touch(self.con, "messenger", "+79120000002", "2026-09-09T10:00:00")
        A.add_registration(self.con, 2, "8 912 000 00 02", "2026-09-10T12:00:00")
        self.run_all()
        self.assertEqual(self.channel_of(2), "messenger")

    def test_window_boundary(self):
        A.add_touch(self.con, "call", "9120000003", "2026-08-31T12:00:00")  # ровно 10 дней — считается
        A.add_registration(self.con, 3, "9120000003", "2026-09-10T12:00:00")
        A.add_touch(self.con, "call", "9120000004", "2026-08-31T11:59:59")  # на секунду раньше — нет
        A.add_registration(self.con, 4, "9120000004", "2026-09-10T12:00:00")
        self.run_all()
        self.assertEqual(self.channel_of(3), "call")
        self.assertEqual(self.channel_of(4), "organic")

    def test_touch_after_registration_ignored(self):
        A.add_touch(self.con, "sms", "9120000005", "2026-09-10T12:00:01")
        A.add_registration(self.con, 5, "9120000005", "2026-09-10T12:00:00")
        self.run_all()
        self.assertEqual(self.channel_of(5), "organic")

    def test_payments_inherit_channel_and_reconcile(self):
        A.add_touch(self.con, "sms", "9120000006", "2026-09-09T10:00:00")
        A.add_registration(self.con, 6, "9120000006", "2026-09-10T12:00:00")
        A.add_payment(self.con, 6, "2026-09-11T09:00:00", 1500)
        A.add_payment(self.con, 6, "2026-09-12T09:00:00", 600)
        report, ok = self.run_all()
        sms = next(r for r in report if r["channel"] == "sms")
        self.assertEqual((sms["regs"], sms["payers"], sms["revenue"]), (1, 1, 2100))
        self.assertTrue(ok["Сверка выручки с оплатами"])


class ReleaseChecks(Base):
    def test_orphan_payment_turns_hold(self):
        A.add_registration(self.con, 7, "9120000007", "2026-09-10T12:00:00")
        A.add_payment(self.con, 999, "2026-09-11T09:00:00", 1500)
        _, ok = self.run_all()
        self.assertFalse(ok["Оплаты без регистрации"])
        self.assertFalse(ok["Сверка выручки с оплатами"])

    def test_silent_day_turns_hold(self):
        uid = 100
        for day in range(1, 9):  # 8 дней по 12 регистраций, 9-го — тишина, хотя касания идут
            for _ in range(12):
                uid += 1
                A.add_registration(self.con, uid, f"9{uid:09d}", f"2026-09-{day:02d}T12:00:00")
        A.add_touch(self.con, "sms", "9000000001", "2026-09-09T12:00:00")
        _, ok = self.run_all()
        self.assertFalse(ok["Источник регистраций не молчит"])

    def test_garbage_phones_turn_hold(self):
        for uid in range(1, 11):
            A.add_registration(self.con, uid, "нет номера" if uid <= 2 else f"9{uid:09d}", "2026-09-10T12:00:00")
        _, ok = self.run_all()
        self.assertFalse(ok["Мусорные телефоны"])


if __name__ == "__main__":
    unittest.main()
