"""Texto predeterminado de las notificaciones de asignacion.

Editar aqui el mensaje (DM al owner y registro en el canal) sin tocar la
logica de asignacion. `details` es el dict que arma
`services.lead_assignment._lead_details` (company, email, phone, country,
job_title, employees, status) — agregar/quitar lineas aqui.
"""

from datetime import datetime, timezone
from typing import Optional


def lead_url(base_url: str, lead_id: str) -> str:
    return f"{base_url.rstrip('/')}/lightning/r/Lead/{lead_id}/view"


def _details_lines(details: Optional[dict]) -> str:
    details = details or {}
    lines = []
    if details.get("company"):
        lines.append(f"*Empresa:* {details['company']}")
    if details.get("job_title"):
        lines.append(f"*Cargo del lead:* {details['job_title']}")
    if details.get("employees"):
        lines.append(f"*N° empleados:* {details['employees']}")
    if details.get("country"):
        lines.append(f"*País:* {details['country']}")
    if details.get("email"):
        lines.append(f"*Email:* {details['email']}")
    if details.get("phone"):
        lines.append(f"*Teléfono:* {details['phone']}")
    if details.get("status"):
        lines.append(f"*Status:* {details['status']}")
    return "\n".join(lines)


def format_owner_dm(lead_name: str, url: str, details: Optional[dict] = None) -> str:
    detail_block = _details_lines(details)
    parts = [
        ":bell: *¡Te asignaron un nuevo lead!*",
        f"*Lead:* <{url}|{lead_name}>",
    ]
    if detail_block:
        parts.append(detail_block)
    parts.append(f"*Asignado:* {_now_label()}")
    parts.append("")
    parts.append("_Mensaje automatico de BOT_ASSIG_LIDER._")
    return "\n".join(parts)


def format_channel_blocks(
    lead_name: str,
    url: str,
    owner_name: str,
    reason: str,
    details: Optional[dict] = None,
    owner_slack_id: Optional[str] = None,
) -> list:
    owner_mention = f"<@{owner_slack_id}>" if owner_slack_id else owner_name
    detail_block = _details_lines(details)
    parts = [
        f"*Lead:* <{url}|{lead_name}>",
        f"*Asignado a:* {owner_mention}",
    ]
    if detail_block:
        parts.append(detail_block)
    parts.append(f"*Fecha:* {_now_label()}")
    parts.append(f"*Detalle:* {reason}")
    body = "\n".join(parts)

    return [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "📋 Nueva asignación de Lead",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": body},
        },
    ]


def format_channel_fallback_text(lead_name: str, owner_name: str) -> str:
    """Texto plano para el parametro `text` de chat.postMessage (fallback de
    notificaciones/accesibilidad cuando los blocks no se pueden renderizar)."""
    return f"Nueva asignación: {lead_name} → {owner_name}"


def _now_label() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
