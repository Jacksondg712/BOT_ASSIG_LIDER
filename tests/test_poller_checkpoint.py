import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.poller import load_checkpoint, load_retry_lead_ids, save_checkpoint, save_retry_lead_ids


class CheckpointTests(unittest.TestCase):
    def test_missing_file_initializes_to_now_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "poll_state.json"
            self.assertFalse(path.exists())

            checkpoint = load_checkpoint(path)

            self.assertTrue(path.exists())
            self.assertEqual(load_checkpoint(path), checkpoint)

    def test_existing_checkpoint_is_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "poll_state.json"
            save_checkpoint("2026-01-01T00:00:00.000+0000", path)

            self.assertEqual(load_checkpoint(path), "2026-01-01T00:00:00.000+0000")

    def test_corrupt_file_is_reset_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "poll_state.json"
            path.write_text("{not valid json", encoding="utf-8")

            checkpoint = load_checkpoint(path)

            self.assertTrue(checkpoint)
            self.assertEqual(load_checkpoint(path), checkpoint)

    def test_retry_list_defaults_to_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_retry_lead_ids(Path(tmp) / "poll_state.json"), [])

    def test_checkpoint_and_retry_list_do_not_overwrite_each_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "poll_state.json"
            save_checkpoint("2026-01-01T00:00:00.000+0000", path)
            save_retry_lead_ids(["00Q1"], path)
            save_checkpoint("2026-01-02T00:00:00.000+0000", path)

            self.assertEqual(load_checkpoint(path), "2026-01-02T00:00:00.000+0000")
            self.assertEqual(load_retry_lead_ids(path), ["00Q1"])

    def test_old_format_without_retry_list_still_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "poll_state.json"
            path.write_text('{"last_created_date": "2026-01-01T00:00:00.000+0000"}', encoding="utf-8")

            self.assertEqual(load_checkpoint(path), "2026-01-01T00:00:00.000+0000")
            self.assertEqual(load_retry_lead_ids(path), [])


if __name__ == "__main__":
    unittest.main()
