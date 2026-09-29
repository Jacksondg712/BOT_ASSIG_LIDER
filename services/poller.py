"""Poller: reemplaza al webhook mientras no se pueda configurar un Flow en
Salesforce (requiere permisos de admin para crear Named/External
Credentials, que hoy no estan disponibles).

En vez de esperar una notificacion, el bot consulta Salesforce cada
`POLL_INTERVAL_SECONDS` buscando Leads que cumplan el segmento objetivo
(ver lead_segment.json) y aun no hayan sido vistos.

Checkpoint: para NO barrer de una el backlog historico de Leads ya
existentes que cumplen el segmento (a la fecha de escribir esto, ~5646),
se guarda en disco la fecha de creacion del ultimo Lead procesado
(`poll_state.json`). Al arrancar por primera vez, el checkpoint se inicializa
en "ahora" — o sea, el bot solo procesa Leads creados DESPUES de que arranco,
nunca el backlog viejo, a menos que se borre `poll_state.json` a mano (ver
README, seccion "Procesar el backlog existente").
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import lead_segment, settings
# Reutiliza la MISMA instancia de SalesforceClient que services.lead_assignment
# (una sola sesion/caché de RecordType y User para todo el proceso).
from services.lead_assignment import handle_lead_ids, salesforce

logger = logging.getLogger(__name__)

_STATE_PATH = Path(__file__).parent.parent / "poll_state.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def load_checkpoint(path: Path = _STATE_PATH) -> str:
    """Devuelve la fecha (ISO, con offset) desde la que hay que buscar Leads.

    Si no existe el archivo (primera corrida), se inicializa en "ahora" y se
    persiste de inmediato, para no arrastrar el backlog historico.
    """
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            checkpoint = data.get("last_created_date")
            if checkpoint:
                return checkpoint
        except (json.JSONDecodeError, OSError):
            logger.warning("poll_state.json invalido, se reinicia el checkpoint.")

    checkpoint = _now_iso()
    save_checkpoint(checkpoint, path)
    logger.info(
        "Checkpoint inicial creado en %s — no se procesara backlog anterior a esta fecha.",
        checkpoint,
    )
    return checkpoint


def save_checkpoint(value: str, path: Path = _STATE_PATH) -> None:
    path.write_text(json.dumps({"last_created_date": value}), encoding="utf-8")


async def _resolve_unassigned_owner_ids() -> list[str]:
    """Resuelve todos los owners que cuentan como 'sin asignar de verdad'
    (ver lead_segment.json → unassigned_owners): el usuario de integracion
    para la mayoria de paises, y Queues especificas para paises con su
    propia asignacion automatica (ej. Brasil)."""
    owner_ids: list[str] = []
    for entry in lead_segment.get("unassigned_owners", []):
        entry_type = (entry.get("type") or "").lower()
        identifier = entry.get("identifier")
        if not identifier:
            continue

        if entry_type == "user":
            owner_id = await salesforce.get_user_id_by_username(identifier)
        elif entry_type == "queue":
            owner_id = await salesforce.get_queue_id_by_name(identifier)
        else:
            logger.warning("unassigned_owners: tipo desconocido '%s' (identifier=%s)", entry_type, identifier)
            continue

        if owner_id:
            owner_ids.append(owner_id)
        else:
            logger.warning(
                "unassigned_owners: no se pudo resolver %s '%s' — se ignora esta entrada.",
                entry_type,
                identifier,
            )

    return owner_ids


async def run_poll_cycle() -> list:
    """Ejecuta un ciclo: busca Leads nuevos del segmento y los procesa.

    Devuelve la lista de LeadAssignmentResult (vacia si no habia nada nuevo).
    """
    checkpoint = load_checkpoint()

    record_type_id = await salesforce.get_record_type_id(
        lead_segment["record_type_developer_name"]
    )
    owner_ids = await _resolve_unassigned_owner_ids()
    if not record_type_id or not owner_ids:
        logger.error(
            "No se pudo resolver record_type_id (%s) u owner_ids (%s) — se "
            "aborta el ciclo de polling.",
            record_type_id,
            owner_ids,
        )
        return []

    candidates = await salesforce.find_unassigned_lead_ids(
        record_type_id=record_type_id,
        owner_ids=owner_ids,
        excluded_statuses=lead_segment.get("excluded_statuses", []),
        since_created_date=checkpoint,
        limit=settings.poll_batch_size,
        utm_source=lead_segment.get("utm_source") or None,
    )

    if not candidates:
        logger.info("Polling: sin Leads nuevos desde %s.", checkpoint)
        return []

    lead_ids = [candidate["Id"] for candidate in candidates]
    logger.info("Polling: %s Lead(s) nuevo(s) encontrados: %s", len(lead_ids), lead_ids)

    results = await handle_lead_ids(lead_ids)
    results_by_id = {result.lead_id: result for result in results}

    # El checkpoint SOLO avanza hasta justo antes del primer Lead que dio
    # error (o que ni siquiera aparece en `results`, lo cual no deberia
    # pasar salvo un bug). Si avanzara sobre un Lead con error, ese Lead
    # jamas se volveria a intentar (CreatedDate > checkpoint lo excluiria
    # para siempre), aunque en Salesforce siga sin asignar. Los que dieron
    # error se reintentan en el proximo ciclo.
    new_checkpoint = None
    for candidate in candidates:
        result = results_by_id.get(candidate["Id"])
        if result is None or result.action == "error":
            break
        new_checkpoint = candidate["CreatedDate"]

    if new_checkpoint:
        save_checkpoint(new_checkpoint)
    else:
        logger.warning(
            "Polling: el primer Lead del lote (%s) fallo — el checkpoint no avanza, "
            "se reintentara en el proximo ciclo.",
            candidates[0]["Id"],
        )

    return results


class PollLoop:
    """Envoltorio simple para poder arrancar/parar el loop desde main.py."""

    def __init__(self):
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    async def run(self, interval_seconds: Optional[int] = None) -> None:
        import asyncio

        interval = interval_seconds or settings.poll_interval_seconds
        logger.info("Poll loop iniciado: cada %s segundos.", interval)
        while not self._stopped:
            try:
                await run_poll_cycle()
            except Exception:
                logger.exception("Error inesperado en el ciclo de polling.")
            await asyncio.sleep(interval)
