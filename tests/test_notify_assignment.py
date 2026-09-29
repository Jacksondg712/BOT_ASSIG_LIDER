import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import services.lead_assignment as lead_assignment


def _run(coro):
    return asyncio.run(coro)


class NotifyAssignmentTests(unittest.TestCase):
    def _owner(self, **overrides):
        owner = {"Id": "005xx", "Name": "Ana Perez", "Email": "ana.perez@openenglish.com"}
        owner.update(overrides)
        return owner

    def _lead(self, **overrides):
        lead = {"Id": "00Q1", "Name": "Lead Uno", "Company": "Acme", "Status": "Prospect"}
        lead.update(overrides)
        return lead

    def test_sends_dm_and_channel_post_when_slack_user_found(self):
        with patch.object(lead_assignment.settings, "slack_notifications_enabled", True), \
             patch.object(lead_assignment.settings, "salesforce_base_url", "https://example.com"), \
             patch.object(type(lead_assignment.settings), "notification_channel_id", "C123", create=True), \
             patch.object(lead_assignment.slack, "find_user_id_by_email", new=AsyncMock(return_value="U123")), \
             patch.object(lead_assignment.slack, "send_dm", new=AsyncMock(return_value=True)) as mock_dm, \
             patch.object(lead_assignment.slack, "post_message", new=AsyncMock(return_value=True)) as mock_post:
            _run(lead_assignment._notify_assignment(self._lead(), self._owner(), "motivo"))

        mock_dm.assert_awaited_once()
        self.assertEqual(mock_dm.await_args.args[0], "U123")
        mock_post.assert_awaited_once()
        self.assertEqual(mock_post.await_args.args[0], "C123")

    def test_skips_dm_but_still_posts_channel_when_slack_user_not_found(self):
        with patch.object(lead_assignment.settings, "slack_notifications_enabled", True), \
             patch.object(lead_assignment.settings, "salesforce_base_url", "https://example.com"), \
             patch.object(type(lead_assignment.settings), "notification_channel_id", "C123", create=True), \
             patch.object(lead_assignment.slack, "find_user_id_by_email", new=AsyncMock(return_value=None)), \
             patch.object(lead_assignment.slack, "send_dm", new=AsyncMock()) as mock_dm, \
             patch.object(lead_assignment.slack, "post_message", new=AsyncMock(return_value=True)) as mock_post:
            _run(lead_assignment._notify_assignment(self._lead(), self._owner(), "motivo"))

        mock_dm.assert_not_called()
        mock_post.assert_awaited_once()

    def test_disabled_notifications_skip_everything(self):
        with patch.object(lead_assignment.settings, "slack_notifications_enabled", False), \
             patch.object(lead_assignment.slack, "find_user_id_by_email", new=AsyncMock()) as mock_lookup, \
             patch.object(lead_assignment.slack, "post_message", new=AsyncMock()) as mock_post:
            _run(lead_assignment._notify_assignment(self._lead(), self._owner(), "motivo"))

        mock_lookup.assert_not_called()
        mock_post.assert_not_called()

    def test_slack_exception_does_not_propagate(self):
        with patch.object(lead_assignment.settings, "slack_notifications_enabled", True), \
             patch.object(lead_assignment.settings, "salesforce_base_url", "https://example.com"), \
             patch.object(type(lead_assignment.settings), "notification_channel_id", "C123", create=True), \
             patch.object(lead_assignment.slack, "find_user_id_by_email", new=AsyncMock(side_effect=RuntimeError("boom"))):
            # No debe lanzar, solo loguear.
            _run(lead_assignment._notify_assignment(self._lead(), self._owner(), "motivo"))


if __name__ == "__main__":
    unittest.main()
