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
    "Argelia", "Pakistán", "India", "Canadá", "Turquía", "Vietnam",
    "Arabia Saudí", "Filipinas", "Alemania",
]


class CountryScopeWithPatchedConfigTests(unittest.TestCase):
    def _reason(self, country, routing=ROUTING):
        with patch.object(la, "team_routing", routing), patch.object(la, "lead_segment", {}):
            return la._country_scope_rejection_reason(country)

    def test_non_latam_countries_are_out_of_scope(self):
        for country in ("Argelia", "Pakistán", "India", "Canadá", "España", "Turquía"):  # sin extras configurados
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


TEAM_ROUTING = {
    "brasil_titles": ["SDR BR"],
    "brasil_keywords": ["brazil", "brasil"],
    "usa_titles": ["SDR US"],
    "usa_keywords": ["usa", "estados unidos"],
    "latam_country_keywords": ["mexico", "colombia"],
    "default_titles": ["SDR LT", "SDR US"],
}


class ExtraCountriesTests(unittest.TestCase):
    """lead_segment.json -> extra_countries: paises que NO son de Latam pero el
    bot gestiona, y a que equipo van."""

    def _with_extras(self, extras):
        return (
            patch.object(la, "team_routing", TEAM_ROUTING),
            patch.object(la, "lead_segment", {"extra_countries": extras}),
        )

    def _titles(self, country, extras):
        a, b = self._with_extras(extras)
        with a, b:
            return la._target_titles_for_country(country)

    def _scope(self, country, extras):
        a, b = self._with_extras(extras)
        with a, b:
            return la._country_scope_rejection_reason(country)

    SPAIN_LATAM = [{"name": "España", "keywords": ["espana", "spain"], "team": "latam"}]

    def test_spain_is_in_scope_and_goes_to_the_latam_pool(self):
        for country in ("España", "Madrid, España", "Spain", "ESPAÑA"):
            self.assertIsNone(self._scope(country, self.SPAIN_LATAM), country)
            self.assertEqual(self._titles(country, self.SPAIN_LATAM), ["SDR LT", "SDR US"], country)

    def test_spain_is_out_of_scope_without_the_extra_entry(self):
        self.assertIsNotNone(self._scope("España", []))

    def test_other_non_latam_countries_stay_out_of_scope(self):
        for country in ("Argelia", "Pakistán", "India"):
            self.assertIsNotNone(self._scope(country, self.SPAIN_LATAM), country)

    def test_extra_country_can_go_to_the_brasil_or_usa_team(self):
        extras = [
            {"name": "Portugal", "keywords": ["portugal"], "team": "brasil"},
            {"name": "Canadá", "keywords": ["canada"], "team": "usa"},
        ]
        self.assertEqual(self._titles("Portugal", extras), ["SDR BR"])
        self.assertEqual(self._titles("Canadá", extras), ["SDR US"])

    def test_extra_country_can_define_a_brand_new_team_with_titles(self):
        extras = [{"name": "Turquía", "keywords": ["turquia"], "titles": ["SDR Turkey"]}]
        self.assertEqual(self._titles("Turquía", extras), ["SDR Turkey"])
        self.assertIsNone(self._scope("Turquía", extras))

    def test_titles_win_over_team_when_both_are_given(self):
        extras = [{"name": "X", "keywords": ["xland"], "team": "latam", "titles": ["SDR X"]}]
        self.assertEqual(self._titles("Xland", extras), ["SDR X"])

    def test_extras_take_priority_over_the_builtin_rules(self):
        extras = [{"name": "Puerto Rico", "keywords": ["puerto rico"], "team": "brasil"}]
        self.assertEqual(self._titles("Puerto Rico, USA", extras), ["SDR BR"])

    def test_first_matching_entry_wins(self):
        extras = [
            {"name": "A", "keywords": ["espana"], "titles": ["SDR A"]},
            {"name": "B", "keywords": ["espana"], "titles": ["SDR B"]},
        ]
        self.assertEqual(self._titles("España", extras), ["SDR A"])

    def test_country_without_an_extra_entry_keeps_the_normal_routing(self):
        self.assertEqual(self._titles("Brasil", self.SPAIN_LATAM), ["SDR BR"])
        self.assertEqual(self._titles("Estados Unidos", self.SPAIN_LATAM), ["SDR US"])
        self.assertEqual(self._titles("México", self.SPAIN_LATAM), ["SDR LT", "SDR US"])

    def test_invalid_team_gives_empty_titles_and_a_warning_instead_of_a_silent_loss(self):
        extras = [{"name": "España", "keywords": ["espana"], "team": "latan"}]  # typo
        with self.assertLogs(la.logger, level="WARNING") as logs:
            self.assertEqual(self._titles("España", extras), [])
        self.assertIn("no es valido", logs.output[0])

    def test_missing_extra_countries_key_is_backward_compatible(self):
        with patch.object(la, "team_routing", TEAM_ROUTING), patch.object(la, "lead_segment", {}):
            self.assertEqual(la._target_titles_for_country("Colombia"), ["SDR LT", "SDR US"])
            self.assertIsNotNone(la._country_scope_rejection_reason("España"))

    def test_entry_without_keywords_matches_nothing(self):
        extras = [{"name": "vacia", "team": "latam"}]
        self.assertIsNotNone(self._scope("España", extras))


class RealExtraCountriesConfigTests(unittest.TestCase):
    """Contra lead_segment.json y team_routing.json REALES."""

    def test_spain_is_accepted_and_routed_to_the_latam_pool(self):
        for country in ("España", "Madrid, España"):
            self.assertIsNone(la._country_scope_rejection_reason(country), country)
            self.assertEqual(la._target_titles_for_country(country), la._configured_titles("default_titles", "default_title"))

    def test_spain_goes_through_the_whole_segment_check(self):
        lead = {
            "RecordType": {"DeveloperName": "B2B_Company"},
            "Owner": {"Username": "store.integration@openenglish.com", "Name": "Store Integration", "Type": "User"},
            "Status": "Prospect",
            "Email": "contacto@empresa.es",
            "Company": "Empresa SL",
            "Country_of_Residence__c": "España",
            "utmSource__c": "google",
            "landingPagesCurrentIndex__c": "www.openenglish.com/para-empresas",
            "LR_Lead_Campaign__c": None,
        }
        self.assertIsNone(la._lead_matches_target_segment(lead))

    def test_pakistan_and_argelia_are_still_skipped(self):
        for country in ("Pakistán", "Argelia"):
            self.assertIsNotNone(la._country_scope_rejection_reason(country), country)


if __name__ == "__main__":
    unittest.main()
