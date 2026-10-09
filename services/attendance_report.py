"""Mensaje de Slack con la asistencia del equipo.

Se envia UNA vez por dia habil de Colombia (lunes a viernes, sin festivos)
cuando se cierra el margen de cada toma configurada en
attendance.json -> report.snapshots (toma + grace_minutes). Con
['08:00'] y grace 30 sale a las 8:30, ya con la asistencia definitiva.

Para no duplicar el mensaje si el bot se reinicia, lo ya enviado se guarda en
`attendance_report_state.json` (solo del dia en curso, no se versiona en git).
Si el bot estaba apagado a la hora del reporte, lo envia al arrancar siempre
que no hayan pasado mas de report.catchup_minutes.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from config import attendance_config, settings, team_routing
from services.attendance import attendance_applies_today, get_presence, report_due_at, _tz
from utils.slack_formatter import format_attendance_report

logger = logging.getLogger(__name__)

_STATE_PATH = Path(__file__).parent.parent / "attendance_report_state.json"
_CHECK_EVERY_SECONDS = 60


def _titles(plural_key: str, singular_key: str) -> list:
    titles = team_routing.get(plural_key)
    if titles:
        return list(titles)
    single = team_routing.get(singular_key)
    return [single] if single else []


def _pools() -> list:
    """(nombre del equipo, titles) de cada ruta de team_routing.json."""
    pools = [
        ("Brasil", _titles("brasil_titles", "brasil_title")),
        ("USA", _titles("usa_titles", "usa_title")),
        ("Latam", _titles("default_titles", "default_title")),
    ]
    return [(name, titles) for name, titles in pools if titles]


def _load_state(path: Path = _STATE_PATH) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict, path: Path = _STATE_PATH) -> None:
    path.write_text(json.dumps(state), encoding="utf-8")


def due_labels(now: datetime, state: Optional[dict] = None) -> list:
    """Tomas cuyo reporte toca enviar ahora: ya cerro su margen, no ha pasado
    el tiempo de gracia de recuperacion y todavia no se envio hoy."""
    report_cfg = attendance_config.get("report", {})
    if not attendance_config.get("enabled") or not report_cfg.get("enabled"):
        return []

    tz = _tz()
    now = now.astimezone(tz)
    if not attendance_applies_today(now.date()):
        return []

    state = _load_state() if state is None else state
    catchup = timedelta(minutes=int(report_cfg.get("catchup_minutes", 120)))
    due = []
    for label in report_cfg.get("snapshots", []):
        due_at = report_due_at(label, now.date(), tz)
        key = f"{now.date().isoformat()}|{label}"
        if due_at <= now <= due_at + catchup and key not in state:
            due.append(label)
    return due


async def build_report(salesforce, now: datetime, snapshot_label: str) -> dict:
    """Estructura con quien estuvo conectado por equipo segun la toma."""
    tz = _tz()
    now = now.astimezone(tz)
    pools = []
    for name, titles in _pools():
        users = await salesforce.find_active_users_by_titles(titles)
        presence = await get_presence([u["Id"] for u in users], salesforce, now, snapshot_label)
        members = [
            {
                "name": user.get("Name", "?"),
                "title": user.get("Title"),
                "connected": bool(presence and presence.is_present(user["Id"])),
                "first_login": presence.first_login.get(user["Id"]) if presence else None,
            }
            for user in users
        ]
        pools.append({"name": name, "titles": titles, "members": members})

    return {
        "snapshot": snapshot_label,
        "day": now.date(),
        "cutoff": report_due_at(snapshot_label, now.date(), tz),
        "pools": pools,
    }


def _channel() -> str:
    return settings.attendance_report_channel_id


async def run_report_if_due(salesforce, slack, now: Optional[datetime] = None) -> list:
    """Envia los reportes pendientes. Devuelve las tomas enviadas. Nunca lanza."""
    try:
        now = now or datetime.now(_tz())
        state = _load_state()
        labels = due_labels(now, state)
        if not labels:
            return []

        channel = _channel()
        if not settings.slack_notifications_enabled or not channel:
            logger.warning("[ASISTENCIA] Reporte pendiente pero Slack no esta configurado (canal/notificaciones).")
            return []

        sent = []
        for label in labels:
            report = await build_report(salesforce, now, label)
            text, blocks = format_attendance_report(report)
            ok = await slack.post_message(channel, text, blocks=blocks)
            if not ok:
                logger.warning("[ASISTENCIA] No se pudo enviar el reporte %s; se reintenta en el proximo ciclo.", label)
                continue

            # Solo se recuerda lo del dia actual; lo viejo se descarta.
            today = now.astimezone(_tz()).date().isoformat()
            state = {key: value for key, value in state.items() if key.startswith(today)}
            state[f"{today}|{label}"] = now.isoformat()
            _save_state(state)
            logger.info("[ASISTENCIA] Reporte de asistencia %s enviado al canal %s.", label, channel)
            sent.append(label)
        return sent
    except Exception:
        logger.exception("[ASISTENCIA] Error inesperado enviando el reporte de asistencia.")
        return []


class AttendanceReportLoop:
    """Revisa cada minuto si toca enviar el reporte (ver run_report_if_due)."""

    def __init__(self, salesforce, slack):
        self._salesforce = salesforce
        self._slack = slack
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    async def run(self) -> None:
        logger.info("[ASISTENCIA] Loop de reporte iniciado (revisa cada %ss).", _CHECK_EVERY_SECONDS)
        while not self._stopped:
            await run_report_if_due(self._salesforce, self._slack)
            await asyncio.sleep(_CHECK_EVERY_SECONDS)
