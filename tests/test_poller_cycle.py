import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import services.poller as poller
from models.schemas import LeadAssignmentResult


def _run(coro):
    return asyncio.run(coro)


def _result(lead_id: str, action: str) -> LeadAssignmentResult:
    return LeadAssignmentResult(lead_id=lead_id, lead_name=lead_id, action=action, reason="test")


class RunPollCycleTests(unittest.TestCase):
    def test_no_candidates_does_not_advance_checkpoint(self):
        with patch.object(poller, "load_checkpoint", return_value="2026-01-01T00:00:00.000+0000"), \
             patch.object(poller, "save_checkpoint") as mock_save, \
             patch.object(poller.salesforce, "get_record_type_id", new=AsyncMock(return_value="012RT")), \
             patch.object(poller, "_resolve_unassigned_owner_ids", new=AsyncMock(return_value=["005OWNER"])), \
             patch.object(poller.salesforce, "find_unassigned_lead_ids", new=AsyncMock(return_value=[])), \
             patch.object(poller, "handle_lead_ids", new=AsyncMock()) as mock_handle:
            results = _run(poller.run_poll_cycle())

        self.assertEqual(results, [])
        mock_handle.assert_not_called()
        mock_save.assert_not_called()

    def test_candidates_are_processed_and_checkpoint_advances_to_max_created_date(self):
        candidates = [
            {"Id": "00Q1", "CreatedDate": "2026-01-02T00:00:00.000+0000"},
            {"Id": "00Q2", "CreatedDate": "2026-01-03T00:00:00.000+0000"},
        ]
        handled = [_result("00Q1", "processed"), _result("00Q2", "processed")]
        with patch.object(poller, "load_checkpoint", return_value="2026-01-01T00:00:00.000+0000"), \
             patch.object(poller, "save_checkpoint") as mock_save, \
             patch.object(poller.salesforce, "get_record_type_id", new=AsyncMock(return_value="012RT")), \
             patch.object(poller, "_resolve_unassigned_owner_ids", new=AsyncMock(return_value=["005OWNER"])), \
             patch.object(poller.salesforce, "find_unassigned_lead_ids", new=AsyncMock(return_value=candidates)), \
             patch.object(poller, "handle_lead_ids", new=AsyncMock(return_value=handled)) as mock_handle:
            results = _run(poller.run_poll_cycle())

        mock_handle.assert_awaited_once_with(["00Q1", "00Q2"])
        mock_save.assert_called_once_with("2026-01-03T00:00:00.000+0000")
        self.assertEqual(results, handled)

    def test_checkpoint_does_not_advance_past_a_failed_lead(self):
        """Un lead con error NO debe hacer avanzar el checkpoint mas alla de
        su propia fecha, para que se reintente en el proximo ciclo en vez de
        perderse (CreatedDate > checkpoint lo excluiria para siempre)."""
        candidates = [
            {"Id": "00Q1", "CreatedDate": "2026-01-02T00:00:00.000+0000"},
            {"Id": "00Q2", "CreatedDate": "2026-01-03T00:00:00.000+0000"},
        ]
        handled = [_result("00Q1", "processed"), _result("00Q2", "error")]
        with patch.object(poller, "load_checkpoint", return_value="2026-01-01T00:00:00.000+0000"), \
             patch.object(poller, "save_checkpoint") as mock_save, \
             patch.object(poller.salesforce, "get_record_type_id", new=AsyncMock(return_value="012RT")), \
             patch.object(poller, "_resolve_unassigned_owner_ids", new=AsyncMock(return_value=["005OWNER"])), \
             patch.object(poller.salesforce, "find_unassigned_lead_ids", new=AsyncMock(return_value=candidates)), \
             patch.object(poller, "handle_lead_ids", new=AsyncMock(return_value=handled)):
            _run(poller.run_poll_cycle())

        mock_save.assert_called_once_with("2026-01-02T00:00:00.000+0000")

    def test_checkpoint_does_not_advance_at_all_if_first_lead_fails(self):
        candidates = [
            {"Id": "00Q1", "CreatedDate": "2026-01-02T00:00:00.000+0000"},
            {"Id": "00Q2", "CreatedDate": "2026-01-03T00:00:00.000+0000"},
        ]
        handled = [_result("00Q1", "error"), _result("00Q2", "processed")]
        with patch.object(poller, "load_checkpoint", return_value="2026-01-01T00:00:00.000+0000"), \
             patch.object(poller, "save_checkpoint") as mock_save, \
             patch.object(poller.salesforce, "get_record_type_id", new=AsyncMock(return_value="012RT")), \
             patch.object(poller, "_resolve_unassigned_owner_ids", new=AsyncMock(return_value=["005OWNER"])), \
             patch.object(poller.salesforce, "find_unassigned_lead_ids", new=AsyncMock(return_value=candidates)), \
             patch.object(poller, "handle_lead_ids", new=AsyncMock(return_value=handled)):
            _run(poller.run_poll_cycle())

        mock_save.assert_not_called()

    def test_missing_record_type_or_owner_aborts_cycle(self):
        with patch.object(poller, "load_checkpoint", return_value="2026-01-01T00:00:00.000+0000"), \
             patch.object(poller.salesforce, "get_record_type_id", new=AsyncMock(return_value=None)), \
             patch.object(poller, "_resolve_unassigned_owner_ids", new=AsyncMock(return_value=["005OWNER"])), \
             patch.object(poller.salesforce, "find_unassigned_lead_ids", new=AsyncMock()) as mock_find, \
             patch.object(poller, "handle_lead_ids", new=AsyncMock()) as mock_handle:
            results = _run(poller.run_poll_cycle())

        self.assertEqual(results, [])
        mock_find.assert_not_called()
        mock_handle.assert_not_called()

    def test_no_owner_ids_resolved_aborts_cycle(self):
        with patch.object(poller, "load_checkpoint", return_value="2026-01-01T00:00:00.000+0000"), \
             patch.object(poller.salesforce, "get_record_type_id", new=AsyncMock(return_value="012RT")), \
             patch.object(poller, "_resolve_unassigned_owner_ids", new=AsyncMock(return_value=[])), \
             patch.object(poller.salesforce, "find_unassigned_lead_ids", new=AsyncMock()) as mock_find, \
             patch.object(poller, "handle_lead_ids", new=AsyncMock()) as mock_handle:
            results = _run(poller.run_poll_cycle())

        self.assertEqual(results, [])
        mock_find.assert_not_called()
        mock_handle.assert_not_called()


class ResolveUnassignedOwnerIdsTests(unittest.TestCase):
    def test_resolves_user_and_queue_entries(self):
        segment = {
            "unassigned_owners": [
                {"type": "User", "identifier": "integrationapi@openenglish.com"},
                {"type": "Queue", "identifier": "Sales Leads B2B Brazil"},
            ]
        }
        with patch.object(poller, "lead_segment", segment), \
             patch.object(poller.salesforce, "get_user_id_by_username", new=AsyncMock(return_value="005USER")), \
             patch.object(poller.salesforce, "get_queue_id_by_name", new=AsyncMock(return_value="00GQUEUE")):
            owner_ids = _run(poller._resolve_unassigned_owner_ids())

        self.assertEqual(owner_ids, ["005USER", "00GQUEUE"])

    def test_skips_entry_that_fails_to_resolve(self):
        segment = {
            "unassigned_owners": [
                {"type": "User", "identifier": "integrationapi@openenglish.com"},
                {"type": "Queue", "identifier": "Queue Que No Existe"},
            ]
        }
        with patch.object(poller, "lead_segment", segment), \
             patch.object(poller.salesforce, "get_user_id_by_username", new=AsyncMock(return_value="005USER")), \
             patch.object(poller.salesforce, "get_queue_id_by_name", new=AsyncMock(return_value=None)):
            owner_ids = _run(poller._resolve_unassigned_owner_ids())

        self.assertEqual(owner_ids, ["005USER"])

    def test_unknown_type_is_ignored(self):
        segment = {"unassigned_owners": [{"type": "Robot", "identifier": "beep-boop"}]}
        with patch.object(poller, "lead_segment", segment):
            owner_ids = _run(poller._resolve_unassigned_owner_ids())

        self.assertEqual(owner_ids, [])


if __name__ == "__main__":
    unittest.main()
