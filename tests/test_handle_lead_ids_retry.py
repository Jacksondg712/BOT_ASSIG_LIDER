import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import services.lead_assignment as lead_assignment
from models.schemas import LeadAssignmentResult


def _run(coro):
    return asyncio.run(coro)


class HandleLeadIdsRetryTests(unittest.TestCase):
    def setUp(self):
        lead_assignment._PROCESSED_LEAD_IDS.clear()

    def test_failed_lead_is_retried_on_next_call(self):
        error_result = LeadAssignmentResult(
            lead_id="00Q1", lead_name="00Q1", action="error", reason="boom"
        )
        with patch.object(lead_assignment, "_process_lead", new=AsyncMock(return_value=error_result)) as mock_process:
            _run(lead_assignment.handle_lead_ids(["00Q1"]))
            _run(lead_assignment.handle_lead_ids(["00Q1"]))

        self.assertEqual(mock_process.await_count, 2)

    def test_successful_lead_is_not_reprocessed_within_same_run(self):
        ok_result = LeadAssignmentResult(
            lead_id="00Q1", lead_name="00Q1", action="processed", reason="ok"
        )
        with patch.object(lead_assignment, "_process_lead", new=AsyncMock(return_value=ok_result)) as mock_process:
            _run(lead_assignment.handle_lead_ids(["00Q1"]))
            _run(lead_assignment.handle_lead_ids(["00Q1"]))

        self.assertEqual(mock_process.await_count, 1)
        self.assertTrue(lead_assignment.was_processed("00Q1"))

    def test_failed_lead_is_not_marked_as_processed(self):
        error_result = LeadAssignmentResult(
            lead_id="00Q1", lead_name="00Q1", action="error", reason="boom"
        )
        with patch.object(lead_assignment, "_process_lead", new=AsyncMock(return_value=error_result)):
            _run(lead_assignment.handle_lead_ids(["00Q1"]))

        self.assertFalse(lead_assignment.was_processed("00Q1"))


if __name__ == "__main__":
    unittest.main()
