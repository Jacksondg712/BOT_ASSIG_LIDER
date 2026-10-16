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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from config import lead_segment, settings
# Reutiliza la MISMA instancia de SalesforceClient que services.lead_assignment
# (una sola sesion/caché de RecordType y User para todo el proceso).
from services.lead_assignment import handle_lead_ids, salesforce, was_processed

logger = logging.getLogger(__name__)

_STATE_PATH = Path(__file__).parent.parent / "poll_state.json"

# lead_id -> ciclos seguidos en que ese Lead (con error) freno el checkpoint.
# En memoria: tras un reinicio el conteo vuelve a empezar, lo cual solo
# retrasa unos ciclos el paso a la lista de reintentos.
_BLOCKED_ATTEMPTS: dict[str, int] = {}


def _sf_datetime(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def _now_iso() -> str:
    return _sf_datetime(datetime.now(timezone.utc))


def _read_state(path: Path) -> dict:
    """Contenido de poll_state.json, o {} si no existe o esta corrupto."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("poll_state.json invalido, se reinicia el estado del poller.")
        return {}
    return data if isinstance(data, dict) else {}


def _update_state(path: Path, **changes) -> None:
    """Actualiza solo las claves indicadas, conservando las demas (checkpoint
    y lista de reintentos viven en el mismo archivo)."""
    data = _read_state(path)
    data.update(changes)
    path.write_text(json.dumps(data), encoding="utf-8")


def load_checkpoint(path: Path = _STATE_PATH) -> str:
    """Devuelve la fecha (ISO, con offset) desde la que hay que buscar Leads.

    Si no existe el archivo (primera corrida), se inicializa en "ahora" y se
    persiste de inmediato, para no arrastrar el backlog historico.
    """
    checkpoint = _read_state(path).get("last_created_date")
    if checkpoint:
        return checkpoint

    checkpoint = _now_iso()
    save_checkpoint(checkpoint, path)
    logger.info(
        "Checkpoint inicial creado en %s — no se procesara backlog anterior a esta fecha.",
        checkpoint,
    )
    return checkpoint


def save_checkpoint(value: str, path: Path = _STATE_PATH) -> None:
    _update_state(path, last_created_date=value)


def load_retry_lead_ids(path: Path = _STATE_PATH) -> list[str]:
    """Leads con error que el checkpoint ya dejo atras: se reintentan en cada
    ciclo hasta que dejen de fallar (ver run_poll_cycle)."""
    return list(_read_state(path).get("retry_lead_ids") or [])


def save_retry_lead_ids(lead_ids: list[str], path: Path = _STATE_PATH) -> None:
    _update_state(path, retry_lead_ids=lead_ids)


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


async def _lookback_lead_ids(record_type_id: str, owner_ids: list[str], checkpoint: str, exclude: list[str]) -> list[str]:
    """Leads del segmento creados en las ultimas `poll_lookback_hours` horas
    que el checkpoint ya dejo atras (CreatedDate <= checkpoint) y siguen sin
    asignar.

    El checkpoint por CreatedDate puede dejar Leads atras sin haberlos visto:
    un Lead que entra al segmento despues de creado (ej. su owner de
    integracion se agrego tarde a lead_segment.json), uno que Salesforce
    confirma despues de otro mas nuevo que ya se proceso, dos Leads con el
    mismo CreatedDate partidos entre ciclos, o un poll_state.json reiniciado.
    Los que ya se asignaron cambiaron de owner y no aparecen aqui; los que
    este proceso ya evaluo (ej. skipped por filtro de calidad) se descartan
    con was_processed. Devuelve como maximo poll_batch_size Ids.
    """
    hours = settings.poll_lookback_hours
    if hours <= 0:
        return []

    since = _sf_datetime(datetime.now(timezone.utc) - timedelta(hours=hours))
    records = await salesforce.find_unassigned_lead_ids(
        record_type_id=record_type_id,
        owner_ids=owner_ids,
        excluded_statuses=lead_segment.get("excluded_statuses", []),
        since_created_date=since,
        until_created_date=checkpoint,
        limit=None,
        utm_source=lead_segment.get("utm_source") or None,
    )
    lead_ids = [
        record["Id"]
        for record in records
        if record["Id"] not in exclude and not was_processed(record["Id"])
    ]
    return lead_ids[: settings.poll_batch_size]


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

    lead_ids = [candidate["Id"] for candidate in candidates]
    lookback_ids = await _lookback_lead_ids(record_type_id, owner_ids, checkpoint, exclude=lead_ids)
    retry_ids = load_retry_lead_ids()
    pending_retry_ids = [lead_id for lead_id in retry_ids if lead_id not in lead_ids + lookback_ids]

    if not candidates and not lookback_ids and not pending_retry_ids:
        logger.info("Polling: sin Leads nuevos desde %s.", checkpoint)
        return []

    if candidates:
        logger.info("Polling: %s Lead(s) nuevo(s) encontrados: %s", len(lead_ids), lead_ids)
    if lookback_ids:
        logger.warning(
            "Polling: %s Lead(s) sin asignar que el checkpoint (%s) habia dejado atras, "
            "recuperados por la revision de las ultimas %sh: %s",
            len(lookback_ids),
            checkpoint,
            settings.poll_lookback_hours,
            lookback_ids,
        )
    if pending_retry_ids:
        logger.info("Polling: reintentando %s Lead(s) con error previo: %s", len(pending_retry_ids), pending_retry_ids)

    results = await handle_lead_ids(lead_ids + lookback_ids + pending_retry_ids)
    results_by_id = {result.lead_id: result for result in results}

    # Los recuperados por el barrido y los reintentos que dan error quedan en
    # la lista de reintentos (asi no se pierden cuando salen de la ventana).
    # Un reintento sale de la lista en cuanto deja de dar error (asignado, o
    # skipped porque alguien ya lo asigno a mano / dejo de cumplir el segmento).
    still_failing = [
        lead_id
        for lead_id in lookback_ids + pending_retry_ids
        if lead_id in results_by_id and results_by_id[lead_id].action == "error"
    ]

    # El checkpoint avanza hasta justo antes del primer Lead que dio error.
    # Si avanzara sobre un Lead con error, ese Lead jamas se volveria a
    # intentar (CreatedDate > checkpoint lo excluiria para siempre), aunque en
    # Salesforce siga sin asignar.
    #
    # Pero si ese Lead sigue fallando `poll_max_lead_retries` ciclos seguidos
    # (ej. su equipo no tiene ningun usuario activo), frenaria a todos los que
    # vienen detras: el lote de POLL_BATCH_SIZE se llenaria con los mismos
    # Leads y los nuevos nunca entrarian. En ese caso el checkpoint lo pasa y
    # el Lead queda en retry_lead_ids, donde se sigue reintentando cada ciclo.
    #
    # Un Lead sin resultado es uno que handle_lead_ids omitio por ya haberlo
    # procesado con exito antes (was_processed): no debe frenar el checkpoint.
    new_checkpoint = None
    for candidate in candidates:
        lead_id = candidate["Id"]
        result = results_by_id.get(lead_id)
        if result is None:
            if not was_processed(lead_id):
                break  # no deberia pasar salvo un bug: no se arriesga a perderlo
        elif result.action == "error":
            attempts = _BLOCKED_ATTEMPTS.get(lead_id, 0) + 1
            if attempts < settings.poll_max_lead_retries:
                _BLOCKED_ATTEMPTS[lead_id] = attempts
                break
            _BLOCKED_ATTEMPTS.pop(lead_id, None)
            still_failing.append(lead_id)
            logger.error(
                "Polling: el Lead %s fallo %s ciclos seguidos (%s) — el checkpoint lo pasa "
                "para no bloquear a los siguientes; queda en retry_lead_ids y se sigue "
                "reintentando cada ciclo.",
                lead_id,
                attempts,
                result.reason,
            )
        else:
            _BLOCKED_ATTEMPTS.pop(lead_id, None)
        new_checkpoint = candidate["CreatedDate"]

    if set(still_failing) != set(retry_ids):
        save_retry_lead_ids(still_failing)

    if new_checkpoint:
        save_checkpoint(new_checkpoint)
    elif candidates:
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
