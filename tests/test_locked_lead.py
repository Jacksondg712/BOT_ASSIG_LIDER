import asyncio
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

import services.lead_assignment as lead_assignment
from services.salesforce_client import SalesforceApiError, SalesforceClient
from tests.test_process_lead_round_robin import SEGMENT, _lead, _no_attendance_filter, _owner


def _run(coro):
    return asyncio.run(coro)


LOCKED_BODY = [{"message": "This record is locked. If you need to edit it, contact your admin.",
                "errorCode": "ENTITY_IS_LOCKED", "fields": []}]


class SalesforceErrorCodeTests(unittest.TestCase):
    def _request_with(self, response):
        client = SalesforceClient()
        session = MagicMock(access_token="t", instance_url="https://example.com")
        http = MagicMock()
        http.__aenter__ = AsyncMock(return_value=http)
        http.__aexit__ = AsyncMock(return_value=False)
        http.request = AsyncMock(return_value=response)
        with patch.object(client, "_ensure_session", new=AsyncMock(return_value=session)), \
             patch("services.salesforce_client.httpx.AsyncClient", return_value=http):
            with self.assertRaises(SalesforceApiError) as ctx:
                _run(client.update_lead_owner("00Q1", "005A"))
        return ctx.exception

    def test_error_code_is_exposed(self):
        exc = self._request_with(httpx.Response(400, json=LOCKED_BODY))
        self.assertEqual(exc.error_code, "ENTITY_IS_LOCKED")
        self.assertIn("ENTITY_IS_LOCKED", str(exc))

    def test_non_json_body_has_no_error_code(self):
        exc = self._request_with(httpx.Response(500, text="<html>boom</html>"))
        self.assertIsNone(exc.error_code)


class LockedLeadTests(unittest.TestCase):
    ROUTING = {"default_titles": ["SDR Inbound LT"]}

    def setUp(self):
        lead_assignment._PROCESSED_LEAD_IDS.clear()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        enter = self.stack.enter_context
        enter(patch.object(lead_assignment, "_filter_present", new=_no_attendance_filter))
        enter(patch.object(lead_assignment, "lead_segment", SEGMENT))
        enter(patch.object(lead_assignment, "team_routing", self.ROUTING))
        enter(patch.object(lead_assignment, "_pick_next_owner", side_effect=lambda title, cands: cands[0]))
        enter(patch.object(lead_assignment.settings, "slack_notifications_enabled", True))
        enter(patch.object(type(lead_assignment.settings), "notification_channel_id", "C123", create=True))
        enter(patch.object(lead_assignment.salesforce, "get_lead",
                           new=AsyncMock(return_value=_lead(Country_of_Residence__c="Venezuela"))))
        enter(patch.object(lead_assignment.salesforce, "find_active_users_by_titles",
                           new=AsyncMock(return_value=[_owner("005A", "Paola Mora", "SDR Inbound LT")])))
        self.mock_post = enter(patch.object(lead_assignment.slack, "post_message", new=AsyncMock(return_value=True)))
        self.mock_assignment_notice = enter(patch.object(lead_assignment, "_notify_assignment", new=AsyncMock()))

    def _fail_update_with(self, error):
        self.stack.enter_context(
            patch.object(lead_assignment.salesforce, "update_lead_owner", new=AsyncMock(side_effect=error))
        )

    def test_locked_lead_is_skipped_not_error(self):
        self._fail_update_with(SalesforceApiError("Salesforce API error 400", error_code="ENTITY_IS_LOCKED"))

        result = _run(lead_assignment._process_lead("00Q1"))

        self.assertEqual(result.action, "skipped")
        self.assertIn("bloqueado", result.reason)
        self.assertIn("Paola Mora", result.reason)

    def test_locked_lead_posts_channel_alert_once_and_is_not_retried(self):
        self._fail_update_with(SalesforceApiError("Salesforce API error 400", error_code="ENTITY_IS_LOCKED"))

        _run(lead_assignment.handle_lead_ids(["00Q1"]))
        _run(lead_assignment.handle_lead_ids(["00Q1"]))

        self.mock_post.assert_awaited_once()
        channel, text = self.mock_post.await_args.args[:2]
        self.assertEqual(channel, "C123")
        self.assertIn("bloqueado", text)
        self.assertIn("Paola Mora", text)
        self.assertTrue(lead_assignment.was_processed("00Q1"))
        self.mock_assignment_notice.assert_not_awaited()

    def test_other_salesforce_errors_still_fail_and_are_retried(self):
        self._fail_update_with(SalesforceApiError("Salesforce API error 500", error_code=None))

        results = _run(lead_assignment.handle_lead_ids(["00Q1"]))

        self.assertEqual(results[0].action, "error")
        self.assertFalse(lead_assignment.was_processed("00Q1"))
        self.mock_post.assert_not_awaited()

    def test_slack_failure_on_alert_does_not_break_processing(self):
        self._fail_update_with(SalesforceApiError("Salesforce API error 400", error_code="ENTITY_IS_LOCKED"))
        self.mock_post.side_effect = RuntimeError("slack caido")

        result = _run(lead_assignment._process_lead("00Q1"))

        self.assertEqual(result.action, "skipped")


if __name__ == "__main__":
    unittest.main()
