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

    def test_owner_matching_any_title_in_list_passes(self):
        owner = {"Id": "005xx", "Name": "Dayana", "Title": "B2B inbound Latam (US)"}
        valid, _ = lead_assignment._validate_owner_title(
            owner, ["B2B inbound Latam", "B2B inbound Latam (US)"]
        )
        self.assertTrue(valid)

    def test_owner_matching_no_title_in_list_fails(self):
        owner = {"Id": "005xx", "Name": "Ana", "Title": "Account Executive"}
        valid, reason = lead_assignment._validate_owner_title(
            owner, ["B2B inbound Latam", "B2B inbound Latam (US)"]
        )
        self.assertFalse(valid)
        self.assertIn("se esperaba alguno de", reason)


class TargetTitlesForCountryTests(unittest.TestCase):
    BR = "B2B inbound Brasil"
    LT = "B2B inbound Latam"
    US = "B2B inbound Latam (US)"

    ROUTING = {
        "brasil_titles": [BR],
        "brasil_keywords": ["brazil", "brasil"],
        "usa_titles": [US],
        "usa_keywords": ["usa", "united states", "estados unidos", "eeuu", "ee.uu"],
        "default_titles": [LT, US],
    }

    def _titles(self, country, routing=None):
        with patch.object(lead_assignment, "team_routing", routing or self.ROUTING):
            return lead_assignment._target_titles_for_country(country)

    # --- Brasil ---
    def test_english_spelling_routes_to_brasil_team(self):
        """Los datos reales llegan a veces en ingles ('Brazil') pese a que
        el equipo/config estan en espanol — brasil_keywords reconoce ambas
        grafias."""
        self.assertEqual(self._titles("Brazil"), [self.BR])

    def test_city_comma_variant_routes_to_brasil_team(self):
        self.assertEqual(self._titles("São Paulo, Brazil"), [self.BR])

    def test_spanish_picklist_brasil_routes_to_brasil_team(self):
        self.assertEqual(self._titles("Brasil"), [self.BR])

    # --- USA: SOLO el titulo (US) ---
    def test_usa_routes_only_to_us_title(self):
        for country in ("USA", "United States", "Estados Unidos de América", "Miami, USA"):
            self.assertEqual(self._titles(country), [self.US], country)

    def test_usa_never_includes_plain_lt_title(self):
        self.assertNotIn(self.LT, self._titles("USA"))

    # --- Resto de Latam: pool (LT) + (LT) (US) ---
    def test_other_latam_country_pool_includes_lt_and_us_titles(self):
        for country in ("Colombia", "Chile", "Venezuela", "Nicaragua", "México"):
            self.assertEqual(self._titles(country), [self.LT, self.US], country)

    def test_missing_country_routes_to_default_pool(self):
        self.assertEqual(self._titles(None), [self.LT, self.US])

    # --- Compatibilidad / orden ---
    def test_legacy_singular_keys_still_work(self):
        """Configs viejas con brasil_title / usa_title / default_title
        (singulares) siguen funcionando."""
        legacy = {
            "brasil_title": self.BR,
            "brasil_keywords": ["brasil"],
            "usa_title": self.US,
            "usa_keywords": ["usa"],
            "default_title": self.LT,
        }
        self.assertEqual(self._titles("Brasil", legacy), [self.BR])
        self.assertEqual(self._titles("USA", legacy), [self.US])
        self.assertEqual(self._titles("Colombia", legacy), [self.LT])

    def test_no_usa_keywords_configured_falls_back_to_default_pool(self):
        routing_without_usa = {
            "brasil_titles": [self.BR],
            "brasil_keywords": ["brazil", "brasil"],
            "default_titles": [self.LT, self.US],
        }
        self.assertEqual(self._titles("USA", routing_without_usa), [self.LT, self.US])

    def test_brasil_is_checked_before_usa_when_both_could_match(self):
        self.assertEqual(self._titles("Brasil"), [self.BR])


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


class QualityFilterTests(unittest.TestCase):
    """Filtros de calidad de dato de produccion (test/openenglish,
    paises restringidos, campanas/eventos, landing page, LR_Lead_Campaign__c)
    — ver lead_segment.json -> quality_filters."""

    BASE_SEGMENT = {
        "record_type_developer_name": "B2B_Company",
        "unassigned_owners": [{"type": "User", "identifier": "integrationapi@openenglish.com"}],
        "excluded_statuses": [],
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
            "Email": "real.contact@acme.com",
            "Company": "Acme Corp",
            "Country_of_Residence__c": "Colombia",
            "utmSource__c": "google",
            "landingPagesCurrentIndex__c": "www.openenglish.com/para-empresas/",
            "LR_Lead_Campaign__c": None,
        }
        lead.update(overrides)
        return lead

    def _segment(self, **quality_filter_overrides):
        filters = {
            "email_company_excluded_keywords": ["test", "openenglish"],
            "excluded_country_keywords": ["cuba", "iraq"],
            "utm_source_excluded_keywords": ["event"],
            "landing_page_required_keywords": ["para-empresas"],
            "landing_page_excluded_keywords": ["leadform"],
            "campaign_field_must_be_empty": True,
        }
        filters.update(quality_filter_overrides)
        return {**self.BASE_SEGMENT, "quality_filters": filters}

    def test_lead_passing_all_quality_filters_is_accepted(self):
        with patch.object(lead_assignment, "lead_segment", self._segment()):
            reason = lead_assignment._lead_matches_target_segment(self._lead())
        self.assertIsNone(reason)

    def test_no_quality_filters_configured_is_a_noop(self):
        with patch.object(lead_assignment, "lead_segment", self.BASE_SEGMENT):
            reason = lead_assignment._lead_matches_target_segment(self._lead(Email="test@openenglish.com"))
        self.assertIsNone(reason)

    def test_test_email_is_rejected(self):
        with patch.object(lead_assignment, "lead_segment", self._segment()):
            reason = lead_assignment._lead_matches_target_segment(self._lead(Email="someone+test@gmail.com"))
        self.assertIsNotNone(reason)
        self.assertIn("calidad", reason)

    def test_openenglish_email_is_rejected(self):
        with patch.object(lead_assignment, "lead_segment", self._segment()):
            reason = lead_assignment._lead_matches_target_segment(self._lead(Email="staff@openenglish.com"))
        self.assertIsNotNone(reason)

    def test_company_containing_test_as_substring_is_not_a_false_positive(self):
        """Regresion: 'Alfredo Testa' / 'Contesta SAS' son empresas/apellidos
        reales que contienen 'test' como substring — el filtro NO debe
        revisar Company, solo Email, para no rechazarlos por error."""
        with patch.object(lead_assignment, "lead_segment", self._segment()):
            reason = lead_assignment._lead_matches_target_segment(
                self._lead(Company="ALFREDO TESTA", Email="real.contact@acme.com")
            )
        self.assertIsNone(reason)

    def test_restricted_country_is_rejected_even_as_substring(self):
        with patch.object(lead_assignment, "lead_segment", self._segment()):
            reason = lead_assignment._lead_matches_target_segment(
                self._lead(Country_of_Residence__c="La Habana, Cuba")
            )
        self.assertIsNotNone(reason)
        self.assertIn("pais restringido", reason)

    def test_event_utm_source_is_rejected(self):
        with patch.object(lead_assignment, "lead_segment", self._segment()):
            reason = lead_assignment._lead_matches_target_segment(self._lead(utmSource__c="facebook-event"))
        self.assertIsNotNone(reason)

    def test_missing_required_landing_page_keyword_is_rejected(self):
        with patch.object(lead_assignment, "lead_segment", self._segment()):
            reason = lead_assignment._lead_matches_target_segment(
                self._lead(landingPagesCurrentIndex__c="www.openenglish.com/promociones/")
            )
        self.assertIsNotNone(reason)
        self.assertIn("para-empresas", reason)

    def test_excluded_landing_page_keyword_is_rejected(self):
        with patch.object(lead_assignment, "lead_segment", self._segment()):
            reason = lead_assignment._lead_matches_target_segment(
                self._lead(landingPagesCurrentIndex__c="www.openenglish.com/para-empresas/leadform/")
            )
        self.assertIsNotNone(reason)

    def test_non_empty_campaign_field_is_rejected(self):
        with patch.object(lead_assignment, "lead_segment", self._segment()):
            reason = lead_assignment._lead_matches_target_segment(
                self._lead(LR_Lead_Campaign__c="CAMP-2026-01")
            )
        self.assertIsNotNone(reason)
        self.assertIn("LR_Lead_Campaign__c", reason)

    def test_campaign_field_check_disabled_accepts_non_empty_value(self):
        with patch.object(lead_assignment, "lead_segment", self._segment(campaign_field_must_be_empty=False)):
            reason = lead_assignment._lead_matches_target_segment(
                self._lead(LR_Lead_Campaign__c="CAMP-2026-01")
            )
        self.assertIsNone(reason)


if __name__ == "__main__":
    unittest.main()
