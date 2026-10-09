import asyncio
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))

import services.attendance as attendance

BOGOTA = ZoneInfo("America/Bogota")

CONFIG = {
    "enabled": True,
    "timezone": "America/Bogota",
    "snapshot_times": ["07:00", "08:00"],
    "grace_minutes": 0,
    "counted_applications": ["Browser"],
}

PAOLA = {"Id": "005PAOLA", "Name": "Paola Mora"}
DAYANA = {"Id": "005DAYANA", "Name": "Dayana Sabogal"}


def _run(coro):
    return asyncio.run(coro)


def _at(hour, minute=0):
    return datetime(2026, 10, 9, hour, minute, tzinfo=BOGOTA)


def _login(user_id, application="Browser"):
    return {"UserId": user_id, "Application": application, "LoginTime": "2026-10-09T12:00:00.000+0000"}


def _sf(logins):
    sf = MagicMock()
    sf.find_successful_logins = AsyncMock(return_value=logins)
    return sf


class AttendanceTestCase(unittest.TestCase):
    def setUp(self):
        attendance._CACHE.clear()
        patcher = patch.object(attendance, "attendance_config", dict(CONFIG))
        patcher.start()
        self.addCleanup(patcher.stop)


class PoolSizeRulesTests(AttendanceTestCase):
    def test_single_person_pool_is_never_filtered_even_if_not_connected(self):
        sf = _sf([])  # nadie conectado
        result = _run(attendance.filter_present([PAOLA], sf, _at(9)))
        self.assertEqual(result.candidates, [PAOLA])
        self.assertFalse(result.applied)
        sf.find_successful_logins.assert_not_called()  # ni siquiera consulta

    def test_two_person_pool_filters_to_connected_only(self):
        sf = _sf([_login("005PAOLA")])
        result = _run(attendance.filter_present([PAOLA, DAYANA], sf, _at(9)))
        self.assertEqual(result.candidates, [PAOLA])
        self.assertTrue(result.applied)
        self.assertEqual(result.absent_names, ["Dayana Sabogal"])
        self.assertIn("1 de 2", result.note)
        self.assertIn("Dayana Sabogal", result.note)

    def test_everyone_connected_keeps_everyone(self):
        sf = _sf([_login("005PAOLA"), _login("005DAYANA")])
        result = _run(attendance.filter_present([PAOLA, DAYANA], sf, _at(9)))
        self.assertEqual(result.candidates, [PAOLA, DAYANA])
        self.assertEqual(result.absent_names, [])

    def test_nobody_connected_falls_back_to_everyone(self):
        sf = _sf([])
        result = _run(attendance.filter_present([PAOLA, DAYANA], sf, _at(9)))
        self.assertEqual(result.candidates, [PAOLA, DAYANA])
        self.assertFalse(result.applied)
        self.assertIn("nadie conectado", result.note)


class LoginCountingTests(AttendanceTestCase):
    def test_slack_and_mobile_logins_do_not_count(self):
        sf = _sf([_login("005PAOLA", "Slack"), _login("005DAYANA", "Salesforce for Android")])
        result = _run(attendance.filter_present([PAOLA, DAYANA], sf, _at(9)))
        # Ninguno tiene login de navegador -> nadie presente -> se reparte entre todos.
        self.assertEqual(result.candidates, [PAOLA, DAYANA])
        self.assertFalse(result.applied)

    def test_only_browser_login_counts_when_mixed_with_slack(self):
        sf = _sf([_login("005PAOLA", "Slack"), _login("005DAYANA", "Browser")])
        result = _run(attendance.filter_present([PAOLA, DAYANA], sf, _at(9)))
        self.assertEqual(result.candidates, [DAYANA])


class SnapshotTimingTests(AttendanceTestCase):
    def test_before_first_snapshot_does_not_filter(self):
        sf = _sf([_login("005PAOLA")])
        result = _run(attendance.filter_present([PAOLA, DAYANA], sf, _at(6, 59)))
        self.assertEqual(result.candidates, [PAOLA, DAYANA])
        self.assertIn("Antes de la primera toma", result.note)
        sf.find_successful_logins.assert_not_called()

    def test_between_snapshots_uses_the_7am_snapshot_window(self):
        sf = _sf([_login("005PAOLA")])
        _run(attendance.filter_present([PAOLA, DAYANA], sf, _at(7, 30)))
        _, since_utc, until_utc = sf.find_successful_logins.await_args.args
        self.assertEqual(since_utc, "2026-10-09T05:00:00Z")  # 00:00 Bogota
        self.assertEqual(until_utc, "2026-10-09T12:00:00Z")  # 07:00 Bogota, NO 07:30

    def test_after_8am_uses_the_8am_snapshot_window(self):
        sf = _sf([_login("005PAOLA")])
        _run(attendance.filter_present([PAOLA, DAYANA], sf, _at(15)))
        _, _, until_utc = sf.find_successful_logins.await_args.args
        self.assertEqual(until_utc, "2026-10-09T13:00:00Z")  # 08:00 Bogota

    def test_grace_minutes_extend_the_cutoff(self):
        cfg = dict(CONFIG, grace_minutes=10)
        sf = _sf([_login("005PAOLA")])
        with patch.object(attendance, "attendance_config", cfg):
            _run(attendance.filter_present([PAOLA, DAYANA], sf, _at(15)))
        _, _, until_utc = sf.find_successful_logins.await_args.args
        self.assertEqual(until_utc, "2026-10-09T13:10:00Z")  # 08:10 Bogota

    def test_closed_snapshot_is_cached_and_not_requeried(self):
        sf = _sf([_login("005PAOLA")])
        _run(attendance.filter_present([PAOLA, DAYANA], sf, _at(15)))
        _run(attendance.filter_present([PAOLA, DAYANA], sf, _at(16)))
        self.assertEqual(sf.find_successful_logins.await_count, 1)


class RobustnessTests(AttendanceTestCase):
    def test_disabled_never_filters(self):
        sf = _sf([_login("005PAOLA")])
        with patch.object(attendance, "attendance_config", dict(CONFIG, enabled=False)):
            result = _run(attendance.filter_present([PAOLA, DAYANA], sf, _at(9)))
        self.assertEqual(result.candidates, [PAOLA, DAYANA])
        sf.find_successful_logins.assert_not_called()

    def test_salesforce_error_fails_open(self):
        sf = MagicMock()
        sf.find_successful_logins = AsyncMock(side_effect=RuntimeError("boom"))
        result = _run(attendance.filter_present([PAOLA, DAYANA], sf, _at(9)))
        self.assertEqual(result.candidates, [PAOLA, DAYANA])
        self.assertIn("no disponible", result.note)



class FindSuccessfulLoginsQueryTests(unittest.TestCase):
    """Regresion: Salesforce rechaza filtrar LoginHistory.Status en el WHERE."""

    def _client(self, records):
        from services.salesforce_client import SalesforceClient

        client = SalesforceClient()
        client.query_all = AsyncMock(return_value=records)
        return client

    def test_status_is_not_in_the_soql_where_clause(self):
        client = self._client([])
        _run(client.find_successful_logins(["005A"], "2026-10-09T05:00:00Z", "2026-10-09T13:00:00Z"))
        soql = client.query_all.await_args.args[0]
        where = soql.split("WHERE", 1)[1]
        self.assertNotIn("Status", where)
        self.assertIn("LoginTime >= 2026-10-09T05:00:00Z", where)

    def test_failed_logins_are_filtered_out_in_python(self):
        client = self._client(
            [
                {"UserId": "005A", "Status": "Success", "Application": "Browser"},
                {"UserId": "005B", "Status": "Invalid Password", "Application": "Browser"},
            ]
        )
        logins = _run(client.find_successful_logins(["005A", "005B"], "x", "y"))
        self.assertEqual([login["UserId"] for login in logins], ["005A"])


if __name__ == "__main__":
    unittest.main()
