"""Asistencia diaria: reparte solo entre quienes estan conectados hoy.

Se "toma asistencia" a las horas de attendance.json (por defecto 07:00 y
08:00, hora de Bogota) mirando el LoginHistory de Salesforce: esta presente
quien tenga un login valido (por defecto, solo web 'Browser') entre las 00:00
del dia y esa hora MAS el margen `grace_minutes` (por defecto 30, para quien
tenga alguna dificultad para conectarse). Un lead que llega entre dos tomas
usa la ultima ya cumplida.

Reglas:
- Solo en DIAS HABILES de Colombia (lunes a viernes, sin festivos). Fuera de
  eso no se filtra a nadie.
- Solo se filtra un pool con 2+ personas. Con 1 sola persona se le asigna
  siempre, este conectada o no (ej. USA y Brasil, que tienen una persona).
- Si NADIE del pool aparece presente, no se filtra (se reparte entre todos):
  es preferible repartir mal a dejar leads sin dueno.
- Antes de la primera toma del dia no se filtra a nadie.
- Falla abierta: si la consulta a Salesforce falla, no se filtra y se loguea.

No necesita scheduler ni archivo de estado: LoginHistory es historico, asi
que la lista de presentes "a las 08:00" se puede calcular en cualquier
momento posterior con el mismo resultado.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from config import attendance_config
from utils.business_days import is_business_day

logger = logging.getLogger(__name__)

# (fecha, toma, ids) -> Presence. Solo se cachean tomas ya "cerradas" (pasada
# la hora + margen), porque entonces el resultado ya no cambia.
_CACHE: dict = {}


@dataclass
class AttendanceResult:
    candidates: list
    applied: bool = False
    note: str = ""
    absent_names: list = field(default_factory=list)


@dataclass
class Presence:
    """Quien estaba conectado segun una toma de asistencia."""

    label: str                      # toma, ej. "08:00"
    cutoff: datetime                # toma + margen: hasta aqui cuenta un login
    first_login: dict = field(default_factory=dict)  # user_id -> datetime (Bogota)

    def is_present(self, user_id: str) -> bool:
        return user_id in self.first_login


def _tz() -> ZoneInfo:
    return ZoneInfo(attendance_config.get("timezone", "America/Bogota"))


def _soql_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_login_time(value: str, tz: ZoneInfo) -> datetime:
    # Salesforce devuelve "2026-10-09T12:55:28.000+0000".
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(tz)


def _snapshot_at(label: str, day, tz: ZoneInfo) -> datetime:
    hour, minute = (int(part) for part in label.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=tz)


def _snapshot_datetimes(now: datetime, tz: ZoneInfo) -> list:
    """Horas de toma de hoy como (etiqueta, datetime con zona), ordenadas."""
    result = [(label, _snapshot_at(label, now.date(), tz)) for label in attendance_config.get("snapshot_times", [])]
    return sorted(result, key=lambda item: item[1])


def latest_snapshot(now: datetime, tz: ZoneInfo) -> Optional[tuple]:
    """(etiqueta, datetime) de la ultima toma ya cumplida hoy, o None."""
    done = [item for item in _snapshot_datetimes(now, tz) if item[1] <= now]
    return done[-1] if done else None


def attendance_applies_today(day) -> bool:
    """True si hoy corre la asistencia (dia habil de Colombia, o si
    business_days_only esta apagado)."""
    if not attendance_config.get("business_days_only", True):
        return True
    return is_business_day(day)


def report_due_at(label: str, day, tz: Optional[ZoneInfo] = None) -> datetime:
    """Cuando se cierra el margen de la toma `label` (toma + grace_minutes):
    el momento en que ya es definitivo quien estuvo presente."""
    grace = timedelta(minutes=int(attendance_config.get("grace_minutes", 0)))
    return _snapshot_at(label, day, tz or _tz()) + grace


async def get_presence(
    user_ids,
    salesforce,
    now: datetime,
    snapshot_label: Optional[str] = None,
) -> Optional[Presence]:
    """Presencia de `user_ids` segun la toma `snapshot_label` (o la ultima
    cumplida si no se indica). None si todavia no hay ninguna toma hoy.
    Puede lanzar si Salesforce falla — el que llama decide si falla abierto."""
    tz = _tz()
    now = now.astimezone(tz)

    if snapshot_label:
        label, snapshot_dt = snapshot_label, _snapshot_at(snapshot_label, now.date(), tz)
        if snapshot_dt > now:
            return None
    else:
        snapshot = latest_snapshot(now, tz)
        if snapshot is None:
            return None
        label, snapshot_dt = snapshot

    cutoff = report_due_at(label, now.date(), tz)
    until = min(now, cutoff)
    day_start = datetime.combine(now.date(), time(0, 0), tzinfo=tz)

    ids = frozenset(user_ids)
    cache_key = (now.date().isoformat(), label, ids)
    cached = _CACHE.get(cache_key)
    if cached is not None:
        return cached

    logins = await salesforce.find_successful_logins(ids, _soql_utc(day_start), _soql_utc(until))
    counted = {app.lower() for app in attendance_config.get("counted_applications", ["Browser"])}
    first_login: dict = {}
    for login in logins:  # vienen ordenados por LoginTime ascendente
        if (login.get("Application") or "").lower() not in counted:
            continue
        first_login.setdefault(login["UserId"], _parse_login_time(login["LoginTime"], tz))

    presence = Presence(label=label, cutoff=cutoff, first_login=first_login)
    if now >= cutoff:
        _CACHE[cache_key] = presence
    return presence


async def filter_present(candidates: list, salesforce, now: Optional[datetime] = None) -> AttendanceResult:
    """Devuelve los candidatos presentes segun la ultima toma de asistencia
    (o todos, si no aplica filtrar). Nunca lanza."""
    if not attendance_config.get("enabled") or len(candidates) < 2:
        return AttendanceResult(candidates=candidates)

    try:
        tz = _tz()
        now = (now or datetime.now(tz)).astimezone(tz)

        if not attendance_applies_today(now.date()):
            return AttendanceResult(
                candidates=candidates,
                note="Dia no habil (fin de semana o festivo en Colombia) — sin filtrar por asistencia.",
            )

        presence = await get_presence([user["Id"] for user in candidates], salesforce, now)
        if presence is None:
            return AttendanceResult(
                candidates=candidates,
                note="Antes de la primera toma de asistencia del dia — sin filtrar.",
            )

        label = presence.label
        present = [user for user in candidates if presence.is_present(user["Id"])]
        absent = [user for user in candidates if not presence.is_present(user["Id"])]

        if not present:
            logger.warning(
                "[ASISTENCIA %s] Nadie del pool aparece conectado (%s) — se reparte entre todos.",
                label,
                ", ".join(user.get("Name", "?") for user in candidates),
            )
            return AttendanceResult(
                candidates=candidates,
                note=f"Asistencia {label}: nadie conectado en el pool — se reparte entre todos.",
            )

        absent_names = [user.get("Name", "?") for user in absent]
        if absent:
            logger.info(
                "[ASISTENCIA %s] Presentes: %s | Ausentes (no se les asigna): %s",
                label,
                ", ".join(user.get("Name", "?") for user in present),
                ", ".join(absent_names),
            )
        note = f"Asistencia {label}: {len(present)} de {len(candidates)} conectados"
        if absent_names:
            note += f" (ausente: {', '.join(absent_names)})"
        return AttendanceResult(candidates=present, applied=True, note=note + ".", absent_names=absent_names)

    except Exception:
        logger.exception("[ASISTENCIA] Error consultando LoginHistory — se reparte sin filtrar.")
        return AttendanceResult(candidates=candidates, note="Asistencia no disponible — sin filtrar.")
