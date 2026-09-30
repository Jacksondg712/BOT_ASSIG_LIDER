import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import services.lead_assignment as lead_assignment


def _run(coro):
    return asyncio.run(coro)


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


class ProcessLeadRoundRobinTests(unittest.TestCase):
    """Confirma que _process_lead realmente delega en _pick_next_owner (el
    reparto equitativo) para decidir el owner, en vez de quedarse siempre
    con el primer candidato que devuelve Salesforce. La logica de rotacion
    en si misma (orden, wraparound, persistencia) ya esta cubierta en
    test_round_robin.py — aqui solo se verifica el cableado."""

    SEGMENT = {
        "record_type_developer_name": "B2B_Company",
        "unassigned_owners": [{"type": "User", "identifier": "integrationapi@openenglish.com"}],
        "excluded_statuses": [],
    }
    ROUTING = {
        "brasil_keywords": ["brazil", "brasil"],
        "brasil_title": "SDR Inbound BR",
        "default_title": "SDR Inbound LT",
    }

    def test_uses_whatever_pick_next_owner_returns(self):
        candidates = [
            _owner("005AAA", "Owner A", "SDR Inbound LT"),
            _owner("005BBB", "Owner B", "SDR Inbound LT"),
        ]
        chosen_in_order = [candidates[0], candidates[1]]

        with patch.object(lead_assignment, "lead_segment", self.SEGMENT), \
             patch.object(lead_assignment, "team_routing", self.ROUTING), \
             patch.object(lead_assignment.settings, "slack_notifications_enabled", False), \
             patch.object(
                 lead_assignment.salesforce,
                 "get_lead",
                 new=AsyncMock(side_effect=[_lead(Id="00Q1"), _lead(Id="00Q2")]),
             ), \
             patch.object(
                 lead_assignment.salesforce, "find_active_users_by_title", new=AsyncMock(return_value=candidates)
             ), \
             patch.object(
                 lead_assignment, "_pick_next_owner", side_effect=chosen_in_order
             ) as mock_pick, \
             patch.object(
                 lead_assignment.salesforce, "update_lead_owner", new=AsyncMock(return_value=True)
             ) as mock_update:
            _run(lead_assignment._process_lead("00Q1"))
            _run(lead_assignment._process_lead("00Q2"))

        # Se llamo pasando el Title resuelto y la lista completa de candidatos.
        for call in mock_pick.call_args_list:
            self.assertEqual(call.args[0], "SDR Inbound LT")
            self.assertEqual(call.args[1], candidates)

        owner_ids_used = [call.args[1] for call in mock_update.await_args_list]
        self.assertEqual(owner_ids_used, ["005AAA", "005BBB"])

    def test_no_candidates_errors_without_calling_pick_next_owner(self):
        with patch.object(lead_assignment, "lead_segment", self.SEGMENT), \
             patch.object(lead_assignment, "team_routing", self.ROUTING), \
             patch.object(lead_assignment.salesforce, "get_lead", new=AsyncMock(return_value=_lead())), \
             patch.object(
                 lead_assignment.salesforce, "find_active_users_by_title", new=AsyncMock(return_value=[])
             ), \
             patch.object(lead_assignment, "_pick_next_owner") as mock_pick:
            result = _run(lead_assignment._process_lead("00Q1"))

        mock_pick.assert_not_called()
        self.assertEqual(result.action, "error")


if __name__ == "__main__":
    unittest.main()
