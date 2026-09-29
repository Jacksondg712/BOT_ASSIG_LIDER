import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.poller import load_checkpoint, save_checkpoint


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


if __name__ == "__main__":
    unittest.main()
