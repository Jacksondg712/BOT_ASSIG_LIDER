import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.slack_formatter import (
    format_channel_blocks,
    format_channel_fallback_text,
    format_owner_dm,
    lead_url,
)


class SlackFormatterTests(unittest.TestCase):
    def test_lead_url_strips_trailing_slash(self):
        url = lead_url("https://openeducation.lightning.force.com/", "00Q123")
        self.assertEqual(
            url, "https://openeducation.lightning.force.com/lightning/r/Lead/00Q123/view"
        )

    def test_owner_dm_includes_lead_link(self):
        text = format_owner_dm("Ana Perez", "https://example.com/lead")
        self.assertIn("https://example.com/lead", text)
        self.assertIn("Ana Perez", text)

    def test_channel_blocks_use_mention_when_slack_id_present(self):
        blocks = format_channel_blocks(
            "Ana Perez", "https://example.com/lead", "Jackson Devia", "motivo", owner_slack_id="U123"
        )
        section_text = blocks[1]["text"]["text"]
        self.assertIn("<@U123>", section_text)
        self.assertNotIn("Jackson Devia", section_text)

    def test_channel_blocks_fall_back_to_name_without_slack_id(self):
        blocks = format_channel_blocks(
            "Ana Perez", "https://example.com/lead", "Jackson Devia", "motivo", owner_slack_id=None
        )
        section_text = blocks[1]["text"]["text"]
        self.assertIn("Jackson Devia", section_text)

    def test_fallback_text_mentions_lead_and_owner(self):
        text = format_channel_fallback_text("Ana Perez", "Jackson Devia")
        self.assertIn("Ana Perez", text)
        self.assertIn("Jackson Devia", text)

    def test_owner_dm_includes_details_when_present(self):
        details = {"company": "Acme Corp", "country": "Colombia", "job_title": "HR Director"}
        text = format_owner_dm("Ana Perez", "https://example.com/lead", details)
        self.assertIn("Acme Corp", text)
        self.assertIn("Colombia", text)
        self.assertIn("HR Director", text)

    def test_owner_dm_omits_detail_lines_when_field_missing(self):
        text = format_owner_dm("Ana Perez", "https://example.com/lead", {"company": None})
        self.assertNotIn("*Empresa:*", text)

    def test_timestamps_use_bogota_timezone_not_utc(self):
        """Todos los SDR estan en Bogota — las fechas de los mensajes NUNCA
        deben quedar en UTC ni en la zona horaria del servidor donde corre
        el bot."""
        import re
        from datetime import datetime
        from zoneinfo import ZoneInfo

        text = format_owner_dm("Ana Perez", "https://example.com/lead")
        match = re.search(r"\*Asignado:\* (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) hora Bogotá", text)
        self.assertIsNotNone(match, f"No se encontro el timestamp esperado en: {text!r}")

        shown = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M")
        expected = datetime.now(ZoneInfo("America/Bogota")).replace(tzinfo=None)
        self.assertLessEqual(abs((shown - expected).total_seconds()), 60)

    def test_channel_blocks_include_details_when_present(self):
        details = {"email": "ana@example.com", "status": "Prospect"}
        blocks = format_channel_blocks(
            "Ana Perez", "https://example.com/lead", "Jackson Devia", "motivo", details=details
        )
        section_text = blocks[1]["text"]["text"]
        self.assertIn("ana@example.com", section_text)
        self.assertIn("Prospect", section_text)


if __name__ == "__main__":
    unittest.main()
