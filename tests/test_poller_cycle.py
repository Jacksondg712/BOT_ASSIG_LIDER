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


class _PollerStateIsolation(unittest.TestCase):
    """Aisla cada test del poll_state.json real y del estado en memoria."""

    retry_ids: list = []

    def setUp(self):
        poller._BLOCKED_ATTEMPTS.clear()
        for target, kwargs in (
            ("load_retry_lead_ids", {"return_value": list(self.retry_ids)}),
            ("save_retry_lead_ids", {}),
        ):
            patcher = patch.object(poller, target, **kwargs)
            mock = patcher.start()
            self.addCleanup(patcher.stop)
            setattr(self, f"mock_{target}", mock)
        patcher = patch.object(poller, "was_processed", return_value=False)
        self.mock_was_processed = patcher.start()
        self.addCleanup(patcher.stop)
        # El barrido hacia atras se prueba aparte (LookbackTests).
        patcher = patch.object(poller.settings, "poll_lookback_hours", 0)
        patcher.start()
        self.addCleanup(patcher.stop)


class RunPollCycleTests(_PollerStateIsolation):
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


def _cycle(candidates, handled, max_retries=3):
    """Corre un ciclo con Salesforce/handle_lead_ids simulados. Devuelve
    (mock de save_checkpoint, mock de handle_lead_ids)."""
    with patch.object(poller, "load_checkpoint", return_value="2026-01-01T00:00:00.000+0000"), \
         patch.object(poller, "save_checkpoint") as mock_save, \
         patch.object(poller.settings, "poll_max_lead_retries", max_retries), \
         patch.object(poller.salesforce, "get_record_type_id", new=AsyncMock(return_value="012RT")), \
         patch.object(poller, "_resolve_unassigned_owner_ids", new=AsyncMock(return_value=["005OWNER"])), \
         patch.object(poller.salesforce, "find_unassigned_lead_ids", new=AsyncMock(return_value=candidates)), \
         patch.object(poller, "handle_lead_ids", new=AsyncMock(return_value=handled)) as mock_handle:
        _run(poller.run_poll_cycle())
    return mock_save, mock_handle


_E1 = {"Id": "00QE", "CreatedDate": "2026-01-02T00:00:00.000+0000"}
_S2 = {"Id": "00QS", "CreatedDate": "2026-01-03T00:00:00.000+0000"}
_N3 = {"Id": "00QN", "CreatedDate": "2026-01-04T00:00:00.000+0000"}


class AlreadyProcessedLeadTests(_PollerStateIsolation):
    def test_lead_omitted_because_already_processed_does_not_block_checkpoint(self):
        """Bug original: un Lead skipped que quedo detras de un error ya esta
        en _PROCESSED_LEAD_IDS; handle_lead_ids lo omite (sin resultado) y el
        checkpoint quedaba frenado para siempre en ese Lead."""
        self.mock_was_processed.side_effect = lambda lead_id: lead_id == "00QS"

        mock_save, _ = _cycle([_S2, _N3], [_result("00QN", "processed")])

        mock_save.assert_called_once_with("2026-01-04T00:00:00.000+0000")

    def test_lead_without_result_and_not_processed_still_blocks(self):
        mock_save, _ = _cycle([_S2, _N3], [_result("00QN", "processed")])

        mock_save.assert_not_called()


class PersistentErrorTests(_PollerStateIsolation):
    def test_error_blocks_checkpoint_until_max_retries_then_moves_to_retry_list(self):
        handled = [_result("00QE", "error"), _result("00QS", "skipped")]

        for _ in range(2):  # max_retries=3: los 2 primeros ciclos frena
            mock_save, _ = _cycle([_E1, _S2], handled)
            mock_save.assert_not_called()
        self.mock_save_retry_lead_ids.assert_not_called()

        mock_save, _ = _cycle([_E1, _S2], handled)

        mock_save.assert_called_once_with("2026-01-03T00:00:00.000+0000")
        self.mock_save_retry_lead_ids.assert_called_once_with(["00QE"])
        self.assertNotIn("00QE", poller._BLOCKED_ATTEMPTS)

    def test_success_resets_blocked_attempts(self):
        _cycle([_E1], [_result("00QE", "error")])
        self.assertEqual(poller._BLOCKED_ATTEMPTS["00QE"], 1)

        mock_save, _ = _cycle([_E1], [_result("00QE", "processed")])

        mock_save.assert_called_once_with("2026-01-02T00:00:00.000+0000")
        self.assertNotIn("00QE", poller._BLOCKED_ATTEMPTS)

    def test_new_leads_behind_persistent_error_are_reached(self):
        """Con el error parqueado, el siguiente lote ya no lo trae y los Leads
        nuevos avanzan el checkpoint."""
        for _ in range(3):
            _cycle([_E1, _S2], [_result("00QE", "error"), _result("00QS", "skipped")])
        self.mock_save_retry_lead_ids.assert_called_once_with(["00QE"])

        self.mock_load_retry_lead_ids.return_value = ["00QE"]
        mock_save, mock_handle = _cycle([_N3], [_result("00QN", "processed"), _result("00QE", "error")])

        mock_handle.assert_awaited_once_with(["00QN", "00QE"])
        mock_save.assert_called_once_with("2026-01-04T00:00:00.000+0000")


class RetryListTests(_PollerStateIsolation):
    retry_ids = ["00QOLD"]

    def test_retry_lead_is_processed_even_without_new_candidates(self):
        mock_save, mock_handle = _cycle([], [_result("00QOLD", "error")])

        mock_handle.assert_awaited_once_with(["00QOLD"])
        mock_save.assert_not_called()
        self.mock_save_retry_lead_ids.assert_not_called()  # sigue igual

    def test_retry_lead_is_removed_once_it_stops_failing(self):
        _, mock_handle = _cycle([_N3], [_result("00QN", "processed"), _result("00QOLD", "processed")])

        mock_handle.assert_awaited_once_with(["00QN", "00QOLD"])
        self.mock_save_retry_lead_ids.assert_called_once_with([])

    def test_retry_lead_skipped_is_removed(self):
        _cycle([], [_result("00QOLD", "skipped")])

        self.mock_save_retry_lead_ids.assert_called_once_with([])

    def test_retry_lead_failing_does_not_block_new_leads(self):
        mock_save, _ = _cycle([_N3], [_result("00QN", "processed"), _result("00QOLD", "error")])

        mock_save.assert_called_once_with("2026-01-04T00:00:00.000+0000")
        self.mock_save_retry_lead_ids.assert_not_called()


_OLD = {"Id": "00QOLD", "CreatedDate": "2025-12-31T10:00:00.000+0000"}


class LookbackTests(_PollerStateIsolation):
    """Barrido hacia atras: Leads que el checkpoint dejo atras y siguen sin asignar."""

    def _cycle(self, new, behind, handled, hours=72):
        """`new` = lo que devuelve la consulta del checkpoint; `behind` = lo
        que devuelve la del barrido (la que trae until_created_date)."""

        async def find(**kwargs):
            return behind if kwargs.get("until_created_date") else new

        self.mock_find = AsyncMock(side_effect=find)
        with patch.object(poller, "load_checkpoint", return_value="2026-01-01T00:00:00.000+0000"), \
             patch.object(poller, "save_checkpoint") as mock_save, \
             patch.object(poller.settings, "poll_lookback_hours", hours), \
             patch.object(poller.settings, "poll_batch_size", 20), \
             patch.object(poller.salesforce, "get_record_type_id", new=AsyncMock(return_value="012RT")), \
             patch.object(poller, "_resolve_unassigned_owner_ids", new=AsyncMock(return_value=["005OWNER"])), \
             patch.object(poller.salesforce, "find_unassigned_lead_ids", new=self.mock_find), \
             patch.object(poller, "handle_lead_ids", new=AsyncMock(return_value=handled)) as mock_handle:
            _run(poller.run_poll_cycle())
        return mock_save, mock_handle

    def _lookback_call(self):
        calls = [c for c in self.mock_find.await_args_list if c.kwargs.get("until_created_date")]
        self.assertEqual(len(calls), 1)
        return calls[0].kwargs

    def test_lead_left_behind_by_checkpoint_is_recovered(self):
        mock_save, mock_handle = self._cycle([], [_OLD], [_result("00QOLD", "processed")])

        mock_handle.assert_awaited_once_with(["00QOLD"])
        mock_save.assert_not_called()  # el barrido nunca mueve el checkpoint

    def test_lookback_query_covers_window_up_to_checkpoint_without_limit(self):
        self._cycle([], [], [], hours=72)

        kwargs = self._lookback_call()
        self.assertEqual(kwargs["until_created_date"], "2026-01-01T00:00:00.000+0000")
        self.assertIsNone(kwargs["limit"])
        since = poller.datetime.strptime(kwargs["since_created_date"], "%Y-%m-%dT%H:%M:%S.000+0000")
        expected = poller.datetime.now(poller.timezone.utc).replace(tzinfo=None) - poller.timedelta(hours=72)
        self.assertLess(abs((since - expected).total_seconds()), 60)

    def test_new_leads_and_recovered_leads_are_handled_together(self):
        handled = [_result("00QN", "processed"), _result("00QOLD", "processed")]
        mock_save, mock_handle = self._cycle([_N3], [_OLD], handled)

        mock_handle.assert_awaited_once_with(["00QN", "00QOLD"])
        mock_save.assert_called_once_with("2026-01-04T00:00:00.000+0000")

    def test_leads_already_evaluated_by_this_process_are_not_reprocessed(self):
        """Un skipped (ej. filtro de calidad) sigue en el segmento para
        siempre; no debe reevaluarse en cada ciclo."""
        self.mock_was_processed.side_effect = lambda lead_id: lead_id == "00QOLD"

        _, mock_handle = self._cycle([], [_OLD], [])

        mock_handle.assert_not_called()

    def test_recovered_leads_are_capped_to_batch_size(self):
        behind = [{"Id": f"00Q{i:03}", "CreatedDate": _OLD["CreatedDate"]} for i in range(25)]
        _, mock_handle = self._cycle([], behind, [])

        self.assertEqual(mock_handle.await_args.args[0], [f"00Q{i:03}" for i in range(20)])

    def test_recovered_lead_with_error_goes_to_retry_list(self):
        self._cycle([], [_OLD], [_result("00QOLD", "error")])

        self.mock_save_retry_lead_ids.assert_called_once_with(["00QOLD"])

    def test_lead_in_retry_list_and_window_is_handled_once(self):
        self.mock_load_retry_lead_ids.return_value = ["00QOLD"]

        _, mock_handle = self._cycle([], [_OLD], [_result("00QOLD", "error")])

        mock_handle.assert_awaited_once_with(["00QOLD"])
        self.mock_save_retry_lead_ids.assert_not_called()  # sigue igual

    def test_disabled_with_zero_hours(self):
        _, mock_handle = self._cycle([], [_OLD], [], hours=0)

        mock_handle.assert_not_called()
        self.assertEqual(
            [c for c in self.mock_find.await_args_list if c.kwargs.get("until_created_date")], []
        )


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
