"""Reparto equitativo (round-robin) entre owners activos que comparten el
mismo Title (ej. varios "Sales Development Representative - Inbound (BR)").

Se persiste en disco (`round_robin_state.json`, uno por Title) para que la
rotacion siga donde quedo aunque el bot se reinicie — igual que el
checkpoint del poller en poll_state.json.
"""

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_STATE_PATH = Path(__file__).parent.parent / "round_robin_state.json"


def _load(path: Path = _STATE_PATH) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("round_robin_state.json invalido, se reinicia la rotacion.")
        return {}


def _save(data: dict, path: Path = _STATE_PATH) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def pick_next(title: str, candidates: list[dict], path: Path = _STATE_PATH) -> Optional[dict]:
    """Elige el siguiente owner en la rotacion de `title` entre `candidates`
    (Users activos con ese mismo Title) y avanza/persiste el puntero.

    Orden estable por Id (no por Name ni LastModifiedDate, que pueden
    cambiar y desordenarian la rotacion). Si el ultimo asignado para este
    Title ya no aparece entre los candidatos actuales (se desactivo, le
    cambiaron el Title, etc.), la rotacion se retoma desde el primero.
    Devuelve None si `candidates` esta vacia (nada que elegir).
    """
    if not candidates:
        return None

    ordered = sorted(candidates, key=lambda user: user["Id"])
    if len(ordered) == 1:
        # Con un solo candidato no hace falta tocar el estado — siempre es el mismo.
        return ordered[0]

    state = _load(path)
    last_id = state.get(title)
    ids = [user["Id"] for user in ordered]

    if last_id in ids:
        next_index = (ids.index(last_id) + 1) % len(ordered)
    else:
        next_index = 0

    chosen = ordered[next_index]
    state[title] = chosen["Id"]
    _save(state, path)
    return chosen
