import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.salesforce_client import SalesforceClient


def _run(coro):
    return asyncio.run(coro)


class FindUnassignedLeadIdsQueryTests(unittest.TestCase):
    def test_utm_source_adds_clause_when_provided(self):
        client = SalesforceClient()
        with patch.object(client, "query_all", new=AsyncMock(return_value=[])) as mock_query:
            _run(
                client.find_unassigned_lead_ids(
                    record_type_id="012RT",
                    owner_ids=["005OWNER"],
                    excluded_statuses=["Dead Lead"],
                    utm_source="test",
                )
            )
        soql = mock_query.await_args.args[0]
        self.assertIn("utmSource__c = 'test'", soql)

    def test_no_utm_source_clause_when_not_provided(self):
        client = SalesforceClient()
        with patch.object(client, "query_all", new=AsyncMock(return_value=[])) as mock_query:
            _run(
                client.find_unassigned_lead_ids(
                    record_type_id="012RT",
                    owner_ids=["005OWNER"],
                    excluded_statuses=["Dead Lead"],
                )
            )
        soql = mock_query.await_args.args[0]
        self.assertNotIn("utmSource__c", soql)

    def test_multiple_owner_ids_build_in_clause(self):
        client = SalesforceClient()
        with patch.object(client, "query_all", new=AsyncMock(return_value=[])) as mock_query:
            _run(
                client.find_unassigned_lead_ids(
                    record_type_id="012RT",
                    owner_ids=["005OWNER", "00GQUEUE"],
                    excluded_statuses=["Dead Lead"],
                )
            )
        soql = mock_query.await_args.args[0]
        self.assertIn("OwnerId IN ('005OWNER', '00GQUEUE')", soql)

    def test_no_owner_ids_returns_empty_without_querying(self):
        client = SalesforceClient()
        with patch.object(client, "query_all", new=AsyncMock(return_value=[])) as mock_query:
            result = _run(
                client.find_unassigned_lead_ids(
                    record_type_id="012RT",
                    owner_ids=[],
                    excluded_statuses=["Dead Lead"],
                )
            )
        self.assertEqual(result, [])
        mock_query.assert_not_called()


if __name__ == "__main__":
    unittest.main()
