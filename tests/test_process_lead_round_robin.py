import asyncio
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import services.lead_assignment as lead_assignment


def _run(coro):
    return asyncio.run(coro)


async def _no_attendance_filter(candidates, salesforce, now=None):
    """Los tests de reparto no deben consultar LoginHistory real."""
    from services.attendance import AttendanceResult

    return AttendanceResult(candidates=candidates)


def _lead(**overrides):
    lead = {
        "Id": "00Q1",
        "Name": "Lead Uno",
        "RecordType": {"DeveloperName": "B2B_Company", "Name": "B2B Company"},
        "Owner": {
            "Username": "integrationapi@openenglish.com",
            "Name": "Salesforce Integration API",
            "Type": "User",
        },
        "OwnerId": "0050Z000008e7RPQAY",
        "Status": "Prospect",
        "Country_of_Residence__c": "Colombia",
    }
    lead.update(overrides)
    return lead


def _owner(user_id: str, name: str, title: str) -> dict:
    return {"Id": user_id, "Name": name, "Email": f"{name.lower()}@openenglish.com", "Title": title}


SEGMENT = {
    "record_type_developer_name": "B2B_Company",
    "unassigned_owners": [{"type": "User", "identifier": "integrationapi@openenglish.com"}],
    "excluded_statuses": [],
}


class ProcessLeadRoundRobinTests(unittest.TestCase):
    """Confirma que _process_lead realmente delega en _pick_next_owner (el
    reparto equitativo) para decidir el owner, en vez de quedarse siempre
    con el primer candidato que devuelve Salesforce. La logica de rotacion
    en si misma (orden, wraparound, persistencia) ya esta cubierta en
    test_round_robin.py — aqui solo se verifica el cableado."""

    ROUTING = {
        "brasil_keywords": ["brazil", "brasil"],
        "brasil_titles": ["SDR Inbound BR"],
        "default_titles": ["SDR Inbound LT", "SDR Inbound LT (US)"],
    }

    def test_uses_whatever_pick_next_owner_returns(self):
        candidates = [
            _owner("005AAA", "Owner A", "SDR Inbound LT"),
            _owner("005BBB", "Owner B", "SDR Inbound LT"),
        ]
        chosen_in_order = [candidates[0], candidates[1]]

        with ExitStack() as stack:
            stack.enter_context(patch.object(lead_assignment, "_filter_present", new=_no_attendance_filter))
            stack.enter_context(patch.object(lead_assignment, "lead_segment", SEGMENT))
            stack.enter_context(patch.object(lead_assignment, "team_routing", self.ROUTING))
            stack.enter_context(patch.object(lead_assignment.settings, "slack_notifications_enabled", False))
            stack.enter_context(
                patch.object(
                    lead_assignment.salesforce,
                    "get_lead",
                    new=AsyncMock(side_effect=[_lead(Id="00Q1"), _lead(Id="00Q2")]),
                )
            )
            stack.enter_context(
                patch.object(
                    lead_assignment.salesforce,
                    "find_active_users_by_titles",
                    new=AsyncMock(return_value=candidates),
                )
            )
            mock_pick = stack.enter_context(
                patch.object(lead_assignment, "_pick_next_owner", side_effect=chosen_in_order)
            )
            mock_update = stack.enter_context(
                patch.object(lead_assignment.salesforce, "update_lead_owner", new=AsyncMock(return_value=True))
            )
            _run(lead_assignment._process_lead("00Q1"))
            _run(lead_assignment._process_lead("00Q2"))

        # Se llamo pasando el pool resuelto y la lista completa de candidatos.
        for call in mock_pick.call_args_list:
            # clave de rotacion = el pool completo (LT + LT (US)), no un Title suelto
            self.assertEqual(call.args[0], "SDR Inbound LT + SDR Inbound LT (US)")
            self.assertEqual(call.args[1], candidates)

        owner_ids_used = [call.args[1] for call in mock_update.await_args_list]
        self.assertEqual(owner_ids_used, ["005AAA", "005BBB"])

    def test_no_candidates_errors_without_calling_pick_next_owner(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(lead_assignment, "_filter_present", new=_no_attendance_filter))
            stack.enter_context(patch.object(lead_assignment, "lead_segment", SEGMENT))
            stack.enter_context(patch.object(lead_assignment, "team_routing", self.ROUTING))
            stack.enter_context(
                patch.object(lead_assignment.salesforce, "get_lead", new=AsyncMock(return_value=_lead()))
            )
            stack.enter_context(
                patch.object(
                    lead_assignment.salesforce, "find_active_users_by_titles", new=AsyncMock(return_value=[])
                )
            )
            mock_pick = stack.enter_context(patch.object(lead_assignment, "_pick_next_owner"))
            result = _run(lead_assignment._process_lead("00Q1"))

        mock_pick.assert_not_called()
        self.assertEqual(result.action, "error")


class LatamAndUsaPoolScenarioTests(unittest.TestCase):
    """Escenario real: Paola tiene (LT), Dayana tiene (LT) (US).
    - Leads de Latam se reparten equitativo entre las DOS.
    - Leads de USA van SOLO a Dayana."""

    ROUTING = {
        "brasil_titles": ["SDR BR"],
        "brasil_keywords": ["brasil"],
        "usa_titles": ["SDR LT (US)"],
        "usa_keywords": ["usa", "estados unidos"],
        "default_titles": ["SDR LT", "SDR LT (US)"],
    }
    USERS = [
        _owner("005PAOLA", "Paola", "SDR LT"),
        _owner("005DAYANA", "Dayana", "SDR LT (US)"),
        _owner("005HEIDY", "Heidy", "SDR BR"),
    ]

    def _run_leads(self, countries):
        import services.round_robin as round_robin

        async def fake_find(titles):
            return [u for u in self.USERS if u["Title"] in set(titles)]

        leads = [_lead(Id=f"00Q{i}", Country_of_Residence__c=c) for i, c in enumerate(countries)]
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "rr.json"

            def pick_with_tmp_state(title, candidates):
                return round_robin.pick_next(title, candidates, state)

            with ExitStack() as stack:
                stack.enter_context(patch.object(lead_assignment, "_filter_present", new=_no_attendance_filter))
                stack.enter_context(patch.object(lead_assignment, "lead_segment", SEGMENT))
                stack.enter_context(patch.object(lead_assignment, "team_routing", self.ROUTING))
                stack.enter_context(patch.object(lead_assignment.settings, "slack_notifications_enabled", False))
                stack.enter_context(
                    patch.object(lead_assignment.salesforce, "get_lead", new=AsyncMock(side_effect=leads))
                )
                stack.enter_context(
                    patch.object(lead_assignment.salesforce, "find_active_users_by_titles", new=fake_find)
                )
                stack.enter_context(
                    patch.object(lead_assignment, "_pick_next_owner", side_effect=pick_with_tmp_state)
                )
                upd = stack.enter_context(
                    patch.object(
                        lead_assignment.salesforce, "update_lead_owner", new=AsyncMock(return_value=True)
                    )
                )
                for lead in leads:
                    _run(lead_assignment._process_lead(lead["Id"]))
        return [call.args[1] for call in upd.await_args_list]

    def test_latam_leads_alternate_between_lt_and_us_titles(self):
        owners = self._run_leads(["Colombia", "Chile", "Venezuela", "México"])
        self.assertEqual(owners, ["005DAYANA", "005PAOLA", "005DAYANA", "005PAOLA"])
        self.assertNotIn("005HEIDY", owners)

    def test_usa_leads_only_go_to_us_title(self):
        owners = self._run_leads(["USA", "Estados Unidos", "Miami, USA"])
        self.assertEqual(set(owners), {"005DAYANA"})

    def test_mixed_flow_us_never_goes_to_paola_and_brasil_goes_to_br(self):
        owners = self._run_leads(["Colombia", "USA", "Brasil", "Chile", "USA"])
        # Latam (2 leads) se reparte entre Paola y Dayana; USA solo a Dayana; Brasil solo a Heidy.
        self.assertEqual(owners[1], "005DAYANA")
        self.assertEqual(owners[4], "005DAYANA")
        self.assertEqual(owners[2], "005HEIDY")
        self.assertEqual({owners[0], owners[3]}, {"005PAOLA", "005DAYANA"})


if __name__ == "__main__":
    unittest.main()
