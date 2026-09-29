import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import services.lead_assignment as lead_assignment


class ValidateOwnerTitleTests(unittest.TestCase):
    def test_owner_with_matching_title_passes(self):
        owner = {"Id": "005xx", "Name": "Ana Perez", "Title": "B2B inbound Latam"}
        valid, reason = lead_assignment._validate_owner_title(owner, "B2B inbound Latam")
        self.assertTrue(valid)
        self.assertEqual(reason, "")

    def test_owner_with_different_title_fails(self):
        owner = {"Id": "005xx", "Name": "Ana Perez", "Title": "Account Executive"}
        valid, reason = lead_assignment._validate_owner_title(owner, "B2B inbound Latam")
        self.assertFalse(valid)
        self.assertIn("se esperaba", reason)

    def test_owner_title_matched_via_profile_fallback(self):
        owner = {
            "Id": "005xx",
            "Name": "Ana Perez",
            "Title": None,
            "Profile": {"Name": "B2B inbound Latam"},
        }
        valid, _ = lead_assignment._validate_owner_title(owner, "B2B inbound Latam")
        self.assertTrue(valid)

    def test_title_match_ignores_accents_and_case(self):
        owner = {"Id": "005xx", "Name": "Ana Perez", "Title": "B2B INBOUND LATAM"}
        valid, _ = lead_assignment._validate_owner_title(owner, "B2B inbound Latam")
        self.assertTrue(valid)


class TargetTitleForCountryTests(unittest.TestCase):
    ROUTING = {
        "brasil_title": "B2B inbound Brasil",
        "brasil_keywords": ["brazil", "brasil"],
        "default_title": "B2B inbound Latam",
    }

    def test_english_spelling_routes_to_brasil_team(self):
        """Los datos reales llegan a veces en ingles ('Brazil') pese a que
        el equipo/config estan en espanol — brasil_keywords reconoce ambas
        grafias."""
        with patch.object(lead_assignment, "team_routing", self.ROUTING):
            title = lead_assignment._target_title_for_country("Brazil")
        self.assertEqual(title, "B2B inbound Brasil")

    def test_city_comma_variant_routes_to_brasil_team(self):
        with patch.object(lead_assignment, "team_routing", self.ROUTING):
            title = lead_assignment._target_title_for_country("São Paulo, Brazil")
        self.assertEqual(title, "B2B inbound Brasil")

    def test_spanish_picklist_brasil_routes_to_brasil_team(self):
        with patch.object(lead_assignment, "team_routing", self.ROUTING):
            title = lead_assignment._target_title_for_country("Brasil")
        self.assertEqual(title, "B2B inbound Brasil")

    def test_other_latam_country_routes_to_default_team(self):
        with patch.object(lead_assignment, "team_routing", self.ROUTING):
            self.assertEqual(lead_assignment._target_title_for_country("Colombia"), "B2B inbound Latam")
            self.assertEqual(lead_assignment._target_title_for_country("Chile"), "B2B inbound Latam")

    def test_missing_country_routes_to_default_team(self):
        with patch.object(lead_assignment, "team_routing", self.ROUTING):
            title = lead_assignment._target_title_for_country(None)
        self.assertEqual(title, "B2B inbound Latam")


class LeadMatchesTargetSegmentTests(unittest.TestCase):
    SEGMENT = {
        "record_type_developer_name": "B2B_Company",
        "unassigned_owners": [
            {"type": "User", "identifier": "integrationapi@openenglish.com"},
            {"type": "Queue", "identifier": "Sales Leads B2B Brazil"},
        ],
        "excluded_statuses": ["Dead lead - Closed", "Dead Lead"],
    }

    def _lead(self, **overrides):
        lead = {
            "RecordType": {"DeveloperName": "B2B_Company", "Name": "B2B Company"},
            "Owner": {
                "Username": "integrationapi@openenglish.com",
                "Name": "Salesforce Integration API",
                "Type": "User",
            },
            "OwnerId": "0050Z000008e7RPQAY",
            "Status": "Prospect",
        }
        lead.update(overrides)
        return lead

    def test_matching_lead_passes(self):
        with patch.object(lead_assignment, "lead_segment", self.SEGMENT):
            reason = lead_assignment._lead_matches_target_segment(self._lead())
        self.assertIsNone(reason)

    def test_wrong_record_type_is_rejected(self):
        with patch.object(lead_assignment, "lead_segment", self.SEGMENT):
            lead = self._lead(RecordType={"DeveloperName": "Student", "Name": "Student"})
            reason = lead_assignment._lead_matches_target_segment(lead)
        self.assertIsNotNone(reason)
        self.assertIn("RecordType", reason)

    def test_already_assigned_owner_is_rejected(self):
        with patch.object(lead_assignment, "lead_segment", self.SEGMENT):
            lead = self._lead(
                Owner={"Username": "angie.galindo@openenglish.com", "Name": "Angie Galindo", "Type": "User"}
            )
            reason = lead_assignment._lead_matches_target_segment(lead)
        self.assertIsNotNone(reason)
        self.assertIn("no esta en la lista", reason)

    def test_brasil_queue_owner_is_accepted_as_unassigned(self):
        with patch.object(lead_assignment, "lead_segment", self.SEGMENT):
            lead = self._lead(Owner={"Name": "Sales Leads B2B Brazil", "Type": "Queue"})
            reason = lead_assignment._lead_matches_target_segment(lead)
        self.assertIsNone(reason)

    def test_other_queue_owner_is_rejected(self):
        with patch.object(lead_assignment, "lead_segment", self.SEGMENT):
            lead = self._lead(Owner={"Name": "Some Other Queue", "Type": "Queue"})
            reason = lead_assignment._lead_matches_target_segment(lead)
        self.assertIsNotNone(reason)
        self.assertIn("no esta en la lista", reason)

    def test_dead_status_is_rejected(self):
        with patch.object(lead_assignment, "lead_segment", self.SEGMENT):
            lead = self._lead(Status="Dead Lead")
            reason = lead_assignment._lead_matches_target_segment(lead)
        self.assertIsNotNone(reason)
        self.assertIn("Status", reason)

    def test_utm_source_filter_ignored_when_not_configured(self):
        with patch.object(lead_assignment, "lead_segment", self.SEGMENT):
            lead = self._lead()
            reason = lead_assignment._lead_matches_target_segment(lead)
        self.assertIsNone(reason)

    def test_utm_source_filter_rejects_non_matching_lead(self):
        segment = {**self.SEGMENT, "utm_source": "test"}
        with patch.object(lead_assignment, "lead_segment", segment):
            lead = self._lead(utmSource__c="google")
            reason = lead_assignment._lead_matches_target_segment(lead)
        self.assertIsNotNone(reason)
        self.assertIn("Modo prueba", reason)

    def test_utm_source_filter_accepts_matching_lead(self):
        segment = {**self.SEGMENT, "utm_source": "test"}
        with patch.object(lead_assignment, "lead_segment", segment):
            lead = self._lead(utmSource__c="test")
            reason = lead_assignment._lead_matches_target_segment(lead)
        self.assertIsNone(reason)


if __name__ == "__main__":
    unittest.main()
