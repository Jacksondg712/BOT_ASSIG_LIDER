import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.round_robin import pick_next


def _user(user_id: str, name: str) -> dict:
    return {"Id": user_id, "Name": name}


class PickNextTests(unittest.TestCase):
    def test_single_candidate_always_returns_itself_without_touching_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "round_robin_state.json"
            candidates = [_user("005AAA", "Solo Owner")]

            first = pick_next("Team X", candidates, path)
            second = pick_next("Team X", candidates, path)

            self.assertEqual(first["Id"], "005AAA")
            self.assertEqual(second["Id"], "005AAA")
            self.assertFalse(path.exists())  # no hace falta persistir con un solo candidato

    def test_two_candidates_alternate_in_stable_id_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "round_robin_state.json"
            # A proposito en orden "al reves" para confirmar que se ordena por Id, no por posicion en la lista.
            candidates = [_user("005BBB", "Owner B"), _user("005AAA", "Owner A")]

            picked = [pick_next("Team X", candidates, path)["Id"] for _ in range(4)]

            self.assertEqual(picked, ["005AAA", "005BBB", "005AAA", "005BBB"])

    def test_three_candidates_wrap_around(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "round_robin_state.json"
            candidates = [_user("005AAA", "A"), _user("005BBB", "B"), _user("005CCC", "C")]

            picked = [pick_next("Team X", candidates, path)["Id"] for _ in range(4)]

            self.assertEqual(picked, ["005AAA", "005BBB", "005CCC", "005AAA"])

    def test_different_titles_rotate_independently(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "round_robin_state.json"
            candidates = [_user("005AAA", "A"), _user("005BBB", "B")]

            first_team_x = pick_next("Team X", candidates, path)["Id"]
            first_team_y = pick_next("Team Y", candidates, path)["Id"]

            self.assertEqual(first_team_x, "005AAA")
            self.assertEqual(first_team_y, "005AAA")  # rotacion propia, no comparte puntero con Team X

    def test_last_assigned_no_longer_in_candidates_restarts_from_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "round_robin_state.json"
            candidates = [_user("005AAA", "A"), _user("005BBB", "B")]
            pick_next("Team X", candidates, path)  # deja el puntero en 005AAA

            # 005AAA ya no esta (se desactivo / le cambiaron el Title).
            new_candidates = [_user("005BBB", "B"), _user("005CCC", "C")]
            chosen = pick_next("Team X", new_candidates, path)

            self.assertEqual(chosen["Id"], "005BBB")

    def test_state_persists_across_separate_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "round_robin_state.json"
            candidates = [_user("005AAA", "A"), _user("005BBB", "B")]

            pick_next("Team X", candidates, path)
            second = pick_next("Team X", candidates, path)

            self.assertEqual(second["Id"], "005BBB")
            self.assertTrue(path.exists())

    def test_empty_candidates_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "round_robin_state.json"
            self.assertIsNone(pick_next("Team X", [], path))


if __name__ == "__main__":
    unittest.main()
