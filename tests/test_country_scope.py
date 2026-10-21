"""Alcance por pais: solo Latam + Brasil + USA. Un pais fuera de alcance se
omite (queda sin asignar). Antes todo pais que no fuera Brasil/USA caia en
Latam por omision y se asignaron leads de Argelia, Pakistan, India, etc."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import services.lead_assignment as la
from config import team_routing as real_team_routing

ROUTING = {
    "brasil_keywords": ["brazil", "brasil"],
    "usa_keywords": ["usa", "united states", "estados unidos"],
    "latam_country_keywords": ["mexico", "colombia", "republica dominicana", "nicaragua", "cuba"],
}

# Paises que llegan de verdad al flujo B2B (consulta a Salesforce, ago-oct 2026).
LATAM_REAL = [
    "México", "Chile", "Venezuela", "Argentina", "Colombia", "Perú", "Ecuador", "Guatemala",
    "República Dominicana", "Costa Rica", "Panamá", "Honduras", "El Salvador", "Uruguay",
    "Paraguay", "Nicaragua", "Bolivia", "Puerto Rico", "Cuba",
]
FUERA_DE_ALCANCE_REAL = [
    "Argelia", "Pakistán", "India", "Canadá", "España", "Turquía", "Vietnam",
    "Arabia Saudí", "Filipinas", "Alemania",
]


class CountryScopeWithPatchedConfigTests(unittest.TestCase):
    def _reason(self, country, routing=ROUTING):
        with patch.object(la, "team_routing", routing):
            return la._country_scope_rejection_reason(country)

    def test_non_latam_countries_are_out_of_scope(self):
        for country in ("Argelia", "Pakistán", "India", "Canadá", "España", "Turquía"):
            reason = self._reason(country)
            self.assertIsNotNone(reason, country)
            self.assertIn("fuera del alcance", reason)

    def test_latam_countries_are_in_scope(self):
        for country in ("México", "Colombia", "Nicaragua", "Cuba", "República Dominicana"):
            self.assertIsNone(self._reason(country), country)

    def test_city_plus_country_strings_are_in_scope(self):
        self.assertIsNone(self._reason("Ciudad de México, México"))
        self.assertIsNone(self._reason("Bogotá, Colombia"))

    def test_brasil_and_usa_are_in_scope_without_being_in_the_latam_list(self):
        for country in ("Brasil", "Brazil", "São Paulo, Brazil", "Estados Unidos de América", "USA", "United States"):
            self.assertIsNone(self._reason(country), country)

    def test_lead_without_country_is_not_skipped(self):
        for empty in (None, "", "   "):
            self.assertIsNone(self._reason(empty), repr(empty))

    def test_no_latam_list_configured_means_no_restriction(self):
        legacy = {"brasil_keywords": ["brasil"], "usa_keywords": ["usa"]}
        self.assertIsNone(self._reason("Argelia", legacy))
        self.assertIsNone(self._reason("Argelia", {**ROUTING, "latam_country_keywords": []}))


class CountryScopeInSegmentTests(unittest.TestCase):
    """El rechazo debe llegar hasta _lead_matches_target_segment (lo que usa el
    poller y _process_lead), con la config REAL."""

    def _lead(self, country):
        return {
            "RecordType": {"DeveloperName": "B2B_Company"},
            "Owner": {"Username": "store.integration@openenglish.com", "Name": "Store Integration", "Type": "User"},
            "Status": "Prospect",
            "Email": "contacto@empresa.com",
            "Company": "Empresa SA",
            "Country_of_Residence__c": country,
            "utmSource__c": "google",
            "landingPagesCurrentIndex__c": "www.openenglish.com/para-empresas",
            "LR_Lead_Campaign__c": None,
        }

    def test_argelia_and_pakistan_are_skipped_with_real_config(self):
        for country in ("Argelia", "Pakistán"):
            reason = la._lead_matches_target_segment(self._lead(country))
            self.assertIsNotNone(reason, country)
            self.assertIn("fuera del alcance", reason)

    def test_every_real_latam_country_is_accepted_with_real_config(self):
        for country in LATAM_REAL:
            self.assertIsNone(la._lead_matches_target_segment(self._lead(country)), country)

    def test_brasil_and_usa_are_accepted_with_real_config(self):
        for country in ("Brasil", "Estados Unidos de América"):
            self.assertIsNone(la._lead_matches_target_segment(self._lead(country)), country)

    def test_every_real_out_of_scope_country_is_skipped_with_real_config(self):
        for country in FUERA_DE_ALCANCE_REAL:
            self.assertIsNotNone(la._lead_matches_target_segment(self._lead(country)), country)

    def test_real_config_has_a_latam_list(self):
        self.assertTrue(real_team_routing.get("latam_country_keywords"))


if __name__ == "__main__":
    unittest.main()
