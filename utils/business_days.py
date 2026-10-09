"""Dias habiles de Colombia: lunes a viernes, excluyendo festivos.

Los festivos los calcula la libreria `holidays` (incluye los trasladados al
lunes por la Ley Emiliani, Semana Santa, etc.), no una lista a mano.
"""

from datetime import date
from functools import lru_cache
from typing import Optional

import holidays


@lru_cache(maxsize=8)
def _colombian_holidays(year: int):
    return holidays.country_holidays("CO", years=[year], language="es")


def holiday_name(day: date) -> Optional[str]:
    """Nombre del festivo colombiano de ese dia, o None si no es festivo."""
    return _colombian_holidays(day.year).get(day)


def is_business_day(day: date) -> bool:
    """True si `day` es lunes-viernes y no es festivo en Colombia."""
    return day.weekday() < 5 and holiday_name(day) is None


def non_business_reason(day: date) -> Optional[str]:
    """Por que `day` no es habil ('sabado', 'domingo' o el nombre del
    festivo), o None si es un dia habil."""
    if day.weekday() == 5:
        return "sábado"
    if day.weekday() == 6:
        return "domingo"
    return holiday_name(day)
