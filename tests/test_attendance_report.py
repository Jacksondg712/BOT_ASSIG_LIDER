import asyncio
import sys
import unittest
from contextlib import ExitStack
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))

import services.attendance as attendance
import services.attendance_report as report
from utils.business_days import holiday_name, is_business_day, non_business_reason
from utils.slack_formatter import format_attendance_report

BOGOTA = ZoneInfo("America/Bogota")

CONFIG = {
    "enabled": True,
    "timezone": "America/Bogota",
    "snapshot_times": ["07:00", "08:00"],
    "grace_minutes": 30,
    "counted_applications": ["Browser"],
    "business_days_only": True,
    "report": {"enabled": True, "snapshots": ["08:00"], "catchup_minutes": 120},
}

ROUTING = {
    "brasil_titles": ["SDR BR"],
    "usa_titles": ["SDR US"],
    "default_titles": ["SDR LT", "SDR US"],
}

PAOLA = {"Id": "005PAOLA", "Name": "Paola Mora", "Title": "SDR LT"}
DAYANA = {"Id": "005DAYANA", "Name": "Dayana Sabogal", "Title": "SDR US"}
HEIDY = {"Id": "005HEIDY", "Name": "Heidy Suarez", "Title": "SDR BR"}

# 2026-10-09 viernes habil | 10 sabado | 11 domingo | 12 lunes festivo (Dia de la Raza) | 13 martes habil
FRIDAY, SATURDAY, SUNDAY, HOLIDAY_MONDAY, TUESDAY = (date(2026, 10, d) for d in (9, 10, 11, 12, 13))


def _run(coro):
    return asyncio.run(coro)


def _at(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=BOGOTA)


def _login(user_id, hour, minute, day=FRIDAY, application="Browser"):
    utc = _at(day, hour, minute).astimezone(ZoneInfo("UTC"))
    return {"UserId": user_id, "Application": application, "LoginTime": utc.strftime("%Y-%m-%dT%H:%M:%S.000+0000")}


class FakeSalesforce:
    """Salesforce simulado: usuarios por titulo y logins fijos (respeta since/until)."""

    def __init__(self, logins):
        self.logins = logins

    async def find_active_users_by_titles(self, titles):
        return [u for u in (PAOLA, DAYANA, HEIDY) if u["Title"] in set(titles)]

    async def find_successful_logins(self, user_ids, since_utc, until_utc):
        return [
            login
            for login in self.logins
            if login["UserId"] in set(user_ids) and since_utc <= login["LoginTime"].replace(".000+0000", "Z") <= until_utc
        ]


class ReportTestCase(unittest.TestCase):
    def setUp(self):
        attendance._CACHE.clear()
        self.state = {}
        stack = ExitStack()
        self.addCleanup(stack.close)
        for module in (attendance, report):
            stack.enter_context(patch.object(module, "attendance_config", CONFIG))
        stack.enter_context(patch.object(report, "team_routing", ROUTING))
        stack.enter_context(patch.object(report, "_load_state", side_effect=lambda: dict(self.state)))
        stack.enter_context(patch.object(report, "_save_state", side_effect=lambda s: self.state.update(s)))
        stack.enter_context(patch.object(report.settings, "slack_notifications_enabled", True))
        stack.enter_context(
            patch.object(type(report.settings), "attendance_report_channel_id", "C-ATT", create=True)
        )
        self.slack = MagicMock()
        self.slack.post_message = AsyncMock(return_value=True)


class BusinessDaysTests(unittest.TestCase):
    def test_weekdays_are_business_days(self):
        self.assertTrue(is_business_day(FRIDAY))
        self.assertTrue(is_business_day(TUESDAY))

    def test_weekend_is_not_business_day(self):
        self.assertFalse(is_business_day(SATURDAY))
        self.assertFalse(is_business_day(SUNDAY))
        self.assertEqual(non_business_reason(SATURDAY), "sábado")
        self.assertEqual(non_business_reason(SUNDAY), "domingo")

    def test_colombian_holiday_on_a_weekday_is_not_business_day(self):
        self.assertFalse(is_business_day(HOLIDAY_MONDAY))  # Dia de la Raza
        self.assertIsNotNone(holiday_name(HOLIDAY_MONDAY))

    def test_known_2026_holidays(self):
        for holiday in (date(2026, 1, 1), date(2026, 1, 12), date(2026, 4, 3), date(2026, 5, 1),
                        date(2026, 7, 20), date(2026, 8, 7), date(2026, 12, 8), date(2026, 12, 25)):
            self.assertFalse(is_business_day(holiday), holiday)

    def test_us_holidays_are_not_colombian_holidays(self):
        self.assertTrue(is_business_day(date(2026, 11, 26)))  # Thanksgiving (jueves): habil en Colombia

    def test_day_after_a_holiday_is_business_day(self):
        self.assertTrue(is_business_day(TUESDAY))


class DueLabelsTests(ReportTestCase):
    def test_not_due_before_the_margin_closes(self):
        self.assertEqual(report.due_labels(_at(FRIDAY, 8, 29), {}), [])

    def test_due_exactly_when_the_margin_closes(self):
        self.assertEqual(report.due_labels(_at(FRIDAY, 8, 30), {}), ["08:00"])

    def test_still_due_during_catchup_window(self):
        self.assertEqual(report.due_labels(_at(FRIDAY, 9, 45), {}), ["08:00"])

    def test_not_due_after_catchup_window(self):
        self.assertEqual(report.due_labels(_at(FRIDAY, 10, 31), {}), [])

    def test_not_due_if_already_sent_today(self):
        self.assertEqual(report.due_labels(_at(FRIDAY, 8, 31), {"2026-10-09|08:00": "x"}), [])

    def test_sent_yesterday_does_not_block_today(self):
        self.assertEqual(report.due_labels(_at(FRIDAY, 8, 31), {"2026-10-08|08:00": "x"}), ["08:00"])

    def test_never_on_weekends(self):
        for day in (SATURDAY, SUNDAY):
            self.assertEqual(report.due_labels(_at(day, 8, 31), {}), [], day)

    def test_never_on_colombian_holidays(self):
        self.assertEqual(report.due_labels(_at(HOLIDAY_MONDAY, 8, 31), {}), [])

    def test_disabled_report_never_due(self):
        cfg = dict(CONFIG, report=dict(CONFIG["report"], enabled=False))
        with patch.object(report, "attendance_config", cfg):
            self.assertEqual(report.due_labels(_at(FRIDAY, 8, 31), {}), [])

    def test_adding_the_7am_snapshot_gives_a_second_report_at_730(self):
        cfg = dict(CONFIG, report=dict(CONFIG["report"], snapshots=["07:00", "08:00"]))
        with patch.object(report, "attendance_config", cfg):
            self.assertEqual(report.due_labels(_at(FRIDAY, 7, 30), {}), ["07:00"])
            self.assertEqual(report.due_labels(_at(FRIDAY, 8, 30), {}), ["07:00", "08:00"])  # el de 7:30 sigue pendiente (catch-up)


class MarginTests(ReportTestCase):
    def test_login_inside_the_30_minute_margin_counts(self):
        sf = FakeSalesforce([_login("005PAOLA", 8, 25)])
        presence = _run(attendance.get_presence(["005PAOLA"], sf, _at(FRIDAY, 8, 30), "08:00"))
        self.assertTrue(presence.is_present("005PAOLA"))

    def test_login_after_the_margin_does_not_count(self):
        sf = FakeSalesforce([_login("005PAOLA", 8, 35)])
        presence = _run(attendance.get_presence(["005PAOLA"], sf, _at(FRIDAY, 8, 45), "08:00"))
        self.assertFalse(presence.is_present("005PAOLA"))

    def test_the_8_05_late_login_that_used_to_fail_now_counts(self):
        """Dayana entro a las 8:05 el 8 oct; con el corte estricto la dejaba
        ausente todo el dia."""
        sf = FakeSalesforce([_login("005DAYANA", 8, 5)])
        presence = _run(attendance.get_presence(["005DAYANA"], sf, _at(FRIDAY, 9), None))
        self.assertTrue(presence.is_present("005DAYANA"))

    def test_first_login_time_is_reported_in_bogota(self):
        sf = FakeSalesforce([_login("005PAOLA", 7, 48), _login("005PAOLA", 9, 10)])
        presence = _run(attendance.get_presence(["005PAOLA"], sf, _at(FRIDAY, 9), "08:00"))
        self.assertEqual(presence.first_login["005PAOLA"].strftime("%H:%M"), "07:48")


class FilterOnNonBusinessDaysTests(ReportTestCase):
    def test_no_filtering_on_saturday(self):
        sf = FakeSalesforce([_login("005PAOLA", 8, 0, SATURDAY)])
        result = _run(attendance.filter_present([PAOLA, DAYANA], sf, _at(SATURDAY, 10)))
        self.assertEqual(result.candidates, [PAOLA, DAYANA])
        self.assertIn("no habil", result.note)

    def test_no_filtering_on_holiday(self):
        sf = FakeSalesforce([_login("005PAOLA", 8, 0, HOLIDAY_MONDAY)])
        result = _run(attendance.filter_present([PAOLA, DAYANA], sf, _at(HOLIDAY_MONDAY, 10)))
        self.assertEqual(result.candidates, [PAOLA, DAYANA])

    def test_filtering_still_applies_on_business_day(self):
        sf = FakeSalesforce([_login("005PAOLA", 7, 50)])
        result = _run(attendance.filter_present([PAOLA, DAYANA], sf, _at(FRIDAY, 10)))
        self.assertEqual(result.candidates, [PAOLA])


class RunReportIfDueTests(ReportTestCase):
    def test_sends_once_with_everyones_status(self):
        sf = FakeSalesforce([_login("005PAOLA", 7, 50), _login("005HEIDY", 7, 40)])
        sent = _run(report.run_report_if_due(sf, self.slack, _at(FRIDAY, 8, 30)))
        self.assertEqual(sent, ["08:00"])
        self.slack.post_message.assert_awaited_once()
        channel, text = self.slack.post_message.await_args.args[:2]
        self.assertEqual(channel, "C-ATT")
        self.assertIn("Brasil 1/1", text)
        self.assertIn("Latam 1/2", text)  # Paola si, Dayana no

    def test_does_not_send_twice_on_the_same_day(self):
        sf = FakeSalesforce([_login("005PAOLA", 7, 50)])
        _run(report.run_report_if_due(sf, self.slack, _at(FRIDAY, 8, 30)))
        again = _run(report.run_report_if_due(sf, self.slack, _at(FRIDAY, 8, 31)))
        self.assertEqual(again, [])
        self.assertEqual(self.slack.post_message.await_count, 1)

    def test_sends_again_next_business_day(self):
        sf = FakeSalesforce([])
        _run(report.run_report_if_due(sf, self.slack, _at(FRIDAY, 8, 30)))
        _run(report.run_report_if_due(sf, self.slack, _at(TUESDAY, 8, 30)))
        self.assertEqual(self.slack.post_message.await_count, 2)

    def test_not_sent_before_margin_closes(self):
        sent = _run(report.run_report_if_due(FakeSalesforce([]), self.slack, _at(FRIDAY, 8, 15)))
        self.assertEqual(sent, [])
        self.slack.post_message.assert_not_awaited()

    def test_not_sent_on_weekend_or_holiday(self):
        for day in (SATURDAY, SUNDAY, HOLIDAY_MONDAY):
            _run(report.run_report_if_due(FakeSalesforce([]), self.slack, _at(day, 8, 31)))
        self.slack.post_message.assert_not_awaited()

    def test_failed_post_is_not_marked_as_sent_and_is_retried(self):
        self.slack.post_message = AsyncMock(side_effect=[False, True])
        sf = FakeSalesforce([])
        first = _run(report.run_report_if_due(sf, self.slack, _at(FRIDAY, 8, 30)))
        second = _run(report.run_report_if_due(sf, self.slack, _at(FRIDAY, 8, 31)))
        self.assertEqual(first, [])
        self.assertEqual(second, ["08:00"])

    def test_no_channel_configured_sends_nothing(self):
        with patch.object(type(report.settings), "attendance_report_channel_id", "", create=True):
            sent = _run(report.run_report_if_due(FakeSalesforce([]), self.slack, _at(FRIDAY, 8, 30)))
        self.assertEqual(sent, [])
        self.slack.post_message.assert_not_awaited()

    def test_never_raises_if_salesforce_fails(self):
        broken = MagicMock()
        broken.find_active_users_by_titles = AsyncMock(side_effect=RuntimeError("boom"))
        sent = _run(report.run_report_if_due(broken, self.slack, _at(FRIDAY, 8, 30)))
        self.assertEqual(sent, [])


class FormatTests(ReportTestCase):
    def _report(self, logins):
        return _run(report.build_report(FakeSalesforce(logins), _at(FRIDAY, 8, 30), "08:00"))

    def test_marks_connected_and_absent(self):
        text, blocks = format_attendance_report(self._report([_login("005PAOLA", 7, 50)]))
        body = "\n".join(b["text"]["text"] for b in blocks if b["type"] == "section")
        self.assertIn("✅ Paola Mora — desde las 07:50", body)
        self.assertIn("❌ Dayana Sabogal — no se ha conectado", body)
        self.assertIn("08:30", body)  # cierre del margen
        self.assertIn("viernes 09/10/2026", blocks[0]["text"]["text"])

    def test_single_person_team_says_always_assigned(self):
        _, blocks = format_attendance_report(self._report([]))
        body = "\n".join(b["text"]["text"] for b in blocks if b["type"] == "section")
        self.assertIn("Equipo de 1 persona: se le asigna siempre", body)

    def test_pool_with_absent_member_says_only_connected_get_leads(self):
        _, blocks = format_attendance_report(self._report([_login("005PAOLA", 7, 50)]))
        body = "\n".join(b["text"]["text"] for b in blocks if b["type"] == "section")
        self.assertIn("se reparten solo entre los conectados", body)

    def test_pool_where_nobody_connected_says_everyone_gets_leads(self):
        _, blocks = format_attendance_report(self._report([]))
        body = "\n".join(b["text"]["text"] for b in blocks if b["type"] == "section")
        self.assertIn("Nadie conectado", body)

    def test_header_clock_matches_the_snapshot_hour(self):
        text, _ = format_attendance_report(self._report([]))
        self.assertTrue(text.startswith("🕗 Asistencia 08:00"))
        afternoon = _run(report.build_report(FakeSalesforce([]), _at(FRIDAY, 13, 30), "13:00"))
        text, _ = format_attendance_report(afternoon)
        self.assertTrue(text.startswith("🕐 Asistencia 13:00"))


class AfternoonSnapshotTests(ReportTestCase):
    """Toma de la 1 pm (13:00 + 30 min de margen), ademas de la de las 8 am."""

    CFG = dict(
        CONFIG,
        snapshot_times=["07:00", "08:00", "13:00"],
        report=dict(CONFIG["report"], snapshots=["08:00", "13:00"]),
    )

    def setUp(self):
        super().setUp()
        for module in (attendance, report):
            patcher = patch.object(module, "attendance_config", self.CFG)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_afternoon_report_due_at_1330_and_not_before(self):
        self.assertEqual(report.due_labels(_at(FRIDAY, 13, 29), {"2026-10-09|08:00": "x"}), [])
        self.assertEqual(report.due_labels(_at(FRIDAY, 13, 30), {"2026-10-09|08:00": "x"}), ["13:00"])

    def test_morning_and_afternoon_reports_are_sent_once_each(self):
        sf = FakeSalesforce([_login("005PAOLA", 7, 50)])
        morning = _run(report.run_report_if_due(sf, self.slack, _at(FRIDAY, 8, 30)))
        midday = _run(report.run_report_if_due(sf, self.slack, _at(FRIDAY, 12, 0)))
        afternoon = _run(report.run_report_if_due(sf, self.slack, _at(FRIDAY, 13, 30)))
        again = _run(report.run_report_if_due(sf, self.slack, _at(FRIDAY, 13, 31)))
        self.assertEqual((morning, midday, afternoon, again), (["08:00"], [], ["13:00"], []))
        self.assertEqual(self.slack.post_message.await_count, 2)

    def test_afternoon_report_counts_people_who_arrived_late(self):
        sf = FakeSalesforce([_login("005PAOLA", 7, 50), _login("005DAYANA", 10, 15)])
        _run(report.run_report_if_due(sf, self.slack, _at(FRIDAY, 8, 30)))
        _run(report.run_report_if_due(sf, self.slack, _at(FRIDAY, 13, 30)))
        morning_text = self.slack.post_message.await_args_list[0].args[1]
        afternoon_text = self.slack.post_message.await_args_list[1].args[1]
        self.assertIn("Latam 1/2", morning_text)
        self.assertIn("Latam 2/2", afternoon_text)

    def test_login_inside_the_afternoon_margin_counts(self):
        sf = FakeSalesforce([_login("005DAYANA", 13, 25)])
        presence = _run(attendance.get_presence(["005DAYANA"], sf, _at(FRIDAY, 13, 30), "13:00"))
        self.assertTrue(presence.is_present("005DAYANA"))

    def test_assignment_after_1pm_includes_late_arrivals(self):
        sf = FakeSalesforce([_login("005PAOLA", 7, 50), _login("005DAYANA", 10, 15)])
        morning = _run(attendance.filter_present([PAOLA, DAYANA], sf, _at(FRIDAY, 11)))
        afternoon = _run(attendance.filter_present([PAOLA, DAYANA], sf, _at(FRIDAY, 14)))
        self.assertEqual(morning.candidates, [PAOLA])
        self.assertEqual(afternoon.candidates, [PAOLA, DAYANA])
        self.assertIn("Asistencia 13:00", afternoon.note)


if __name__ == "__main__":
    unittest.main()
