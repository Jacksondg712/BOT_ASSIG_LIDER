"""Tests contra el lead_segment.json REAL (no un fixture): protegen la lista de
owners "sin asignar" — un owner que falte aqui hace INVISIBLES a sus leads para
el bot (paso con Store Integration el 2026-10-14)."""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import services.lead_assignment as lead_assignment
import services.poller as poller
from config import lead_segment

STORE_INTEGRATION = {
    "Username": "store.integration@openenglish.com",
    "Name": "Store Integration",
    "Type": "User",
}
SALESFORCE_INTEGRATION_API = {
    "Username": "integrationapi@openenglish.com",
    "Name": "Salesforce Integration API",
    "Type": "User",
}
BRAZIL_QUEUE = {"Name": "Sales Leads B2B Brazil", "Type": "Queue"}


def _run(coro):
    return asyncio.run(coro)


def _lead(owner):
    return {
        "RecordType": {"DeveloperName": "B2B_Company"},
        "Owner": owner,
        "Status": "Prospect",
        "Email": "contacto@empresa.com",
        "Company": "Empresa SA",
        "Country_of_Residence__c": "Argentina",
        "utmSource__c": "google",
        "landingPagesCurrentIndex__c": "www.openenglish.com/para-empresas",
        "LR_Lead_Campaign__c": None,
    }


class RealUnassignedOwnersTests(unittest.TestCase):
    def test_config_lists_both_integration_users_and_the_brazil_queue(self):
        identifiers = {entry["identifier"] for entry in lead_segment["unassigned_owners"]}
        self.assertIn("integrationapi@openenglish.com", identifiers)
        self.assertIn("store.integration@openenglish.com", identifiers)
        self.assertIn("Sales Leads B2B Brazil", identifiers)

    def test_lead_owned_by_store_integration_is_in_the_segment(self):
        self.assertIsNone(lead_assignment._lead_matches_target_segment(_lead(STORE_INTEGRATION)))

    def test_lead_owned_by_salesforce_integration_api_is_still_in_the_segment(self):
        self.assertIsNone(lead_assignment._lead_matches_target_segment(_lead(SALESFORCE_INTEGRATION_API)))

    def test_lead_in_brazil_queue_is_still_in_the_segment(self):
        self.assertIsNone(lead_assignment._lead_matches_target_segment(_lead(BRAZIL_QUEUE)))

    def test_lead_owned_by_a_regular_person_is_not_in_the_segment(self):
        person = {"Username": "paola.mora@openenglish.com", "Name": "Paola Mora", "Type": "User"}
        self.assertIsNotNone(lead_assignment._lead_matches_target_segment(_lead(person)))

    def test_poller_resolves_an_id_for_every_configured_owner(self):
        async def fake_user(username):
            return f"005::{username}"

        async def fake_queue(name):
            return f"00G::{name}"

        with patch.object(poller.salesforce, "get_user_id_by_username", new=fake_user), \
             patch.object(poller.salesforce, "get_queue_id_by_name", new=fake_queue):
            owner_ids = _run(poller._resolve_unassigned_owner_ids())

        self.assertEqual(
            owner_ids,
            [
                "005::integrationapi@openenglish.com",
                "005::store.integration@openenglish.com",
                "00G::Sales Leads B2B Brazil",
            ],
        )

    def test_soql_looks_for_leads_owned_by_all_three_owners(self):
        client = lead_assignment.SalesforceClient()
        with patch.object(client, "query_all", new=AsyncMock(return_value=[])) as mock_query:
            _run(
                client.find_unassigned_lead_ids(
                    record_type_id="012RT",
                    owner_ids=["005INTEGRATION", "005STORE", "00GBRAZIL"],
                    excluded_statuses=["Dead Lead"],
                )
            )
        soql = mock_query.await_args.args[0]
        self.assertIn("OwnerId IN ('005INTEGRATION', '005STORE', '00GBRAZIL')", soql)


if __name__ == "__main__":
    unittest.main()
