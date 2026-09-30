import logging
import unicodedata
from typing import List, Optional

from config import lead_segment, settings, team_routing
from models.schemas import LeadAssignmentResult
from services.round_robin import pick_next as _pick_next_owner
from services.salesforce_client import SalesforceApiError, SalesforceAuthError, SalesforceClient
from services.slack_service import SlackService
from utils.slack_formatter import (
    format_channel_blocks,
    format_channel_fallback_text,
    format_owner_dm,
    lead_url,
)

logger = logging.getLogger(__name__)

salesforce = SalesforceClient()
slack = SlackService()
_PROCESSED_LEAD_IDS: set[str] = set()


async def handle_lead_ids(lead_ids: List[str]) -> List[LeadAssignmentResult]:
    results: List[LeadAssignmentResult] = []
    for lead_id in lead_ids:
        if lead_id in _PROCESSED_LEAD_IDS:
            logger.info("Lead %s ya fue procesado con exito — se ignora duplicado.", lead_id)
            continue

        try:
            result = await _process_lead(lead_id)
        except (SalesforceAuthError, SalesforceApiError) as exc:
            logger.exception("Error Salesforce procesando lead %s", lead_id)
            result = LeadAssignmentResult(
                lead_id=lead_id,
                lead_name=lead_id,
                action="error",
                reason=str(exc),
            )
        except Exception as exc:
            logger.exception("Error inesperado procesando lead %s", lead_id)
            result = LeadAssignmentResult(
                lead_id=lead_id,
                lead_name=lead_id,
                action="error",
                reason=f"Error inesperado: {exc}",
            )

        # Solo se recuerda como "ya procesado" si NO fue error, para que un
        # lead que fallo (ej. owner mal configurado) se reintente en la
        # proxima llamada (poller o webhook) en vez de perderse para
        # siempre dentro de la vida de este proceso.
        if result.action != "error":
            _PROCESSED_LEAD_IDS.add(lead_id)
        results.append(result)
        logger.info(
            "Resultado lead %s: action=%s reason=%s",
            lead_id,
            result.action,
            result.reason,
        )
    return results


async def _process_lead(lead_id: str) -> LeadAssignmentResult:
    lead = await salesforce.get_lead(lead_id)
    if not lead:
        return LeadAssignmentResult(
            lead_id=lead_id,
            lead_name=lead_id,
            action="error",
            reason="No encontre el Lead en Salesforce.",
        )

    lead_name = lead.get("Name") or lead_id

    # Defensa en profundidad: aunque el disparo del lado de Salesforce (Flow
    # u Outbound Message) ya deberia filtrar por este mismo criterio, se
    # revalida aqui por si el Flow cambia, se prueba a mano, o llega un Id
    # que no corresponde al segmento. Criterio extraido via API del reporte
    # "Copia de Leads-Created-B2B_All-Unassigne" (Id 00OWP000003xuHN2AY):
    # RecordType = B2B Company + Owner = usuario de integracion + Status no
    # muerto. Ver lead_segment.json para editar sin tocar codigo.
    segment_reason = _lead_matches_target_segment(lead)
    if segment_reason:
        return LeadAssignmentResult(
            lead_id=lead_id,
            lead_name=lead_name,
            action="skipped",
            reason=f"El Lead no cumple el criterio del segmento objetivo: {segment_reason}",
        )

    country = lead.get("Country_of_Residence__c")
    target_title = _target_title_for_country(country)
    candidates = await salesforce.find_active_users_by_title(target_title)
    if not candidates:
        return LeadAssignmentResult(
            lead_id=lead_id,
            lead_name=lead_name,
            action="error",
            reason=(
                f"No encontre un usuario activo en Salesforce con Title "
                f"'{target_title}' (equipo resuelto para el pais '{country}'). "
                "Verifica que ese equipo tenga al menos una persona activa "
                "con ese Title en team_routing.json."
            ),
        )

    # Si hay mas de un owner activo con ese Title, se reparte equitativo
    # (round-robin) en vez de siempre caer en el mismo — ver
    # services/round_robin.py. Con un solo candidato, devuelve siempre ese.
    #
    # Nota: el puntero de rotacion avanza aqui, al elegir, no recien cuando
    # se confirma el PATCH en Salesforce. Si algo falla despues (Title
    # invalido, error de Salesforce), este intento "gasto su turno" en la
    # rotacion igual — se acepto este trade-off por simplicidad, ya que esos
    # fallos deberian ser raros/transitorios y el lead se reintenta solo en
    # el siguiente ciclo (con el siguiente owner en turno).
    owner = _pick_next_owner(target_title, candidates)

    valid, reason = _validate_owner_title(owner, target_title)
    if not valid:
        return LeadAssignmentResult(
            lead_id=lead_id,
            lead_name=lead_name,
            action="error",
            reason=reason,
        )

    owner_id = owner["Id"]
    owner_name = owner.get("Name") or owner_id
    current_owner_id = lead.get("OwnerId")

    if owner_id == current_owner_id:
        return LeadAssignmentResult(
            lead_id=lead_id,
            lead_name=lead_name,
            action="processed",
            reason=f"El lead ya pertenece a {owner_name}; no fue necesario actualizar OwnerId.",
        )

    updated = await salesforce.update_lead_owner(lead_id, owner_id)
    if not updated:
        return LeadAssignmentResult(
            lead_id=lead_id,
            lead_name=lead_name,
            action="error",
            reason=f"No se pudo asignar el lead a {owner_name}.",
        )

    success_reason = (
        f"Asignado a {owner_name} (equipo '{target_title}', pais '{country}'). "
        f"Titulo verificado."
    )
    await _notify_assignment(lead, owner, success_reason)

    return LeadAssignmentResult(
        lead_id=lead_id,
        lead_name=lead_name,
        action="processed",
        reason=success_reason,
    )


async def _notify_assignment(lead: dict, owner: dict, reason: str) -> None:
    """DM al owner + registro en el canal de Slack. Nunca lanza: un fallo de
    Slack (token/scope/canal mal configurado) no debe hacer fallar la
    asignacion en Salesforce, que ya quedo escrita."""
    if not settings.slack_notifications_enabled:
        return

    lead_id = lead.get("Id", "")
    lead_name = lead.get("Name") or lead_id
    owner_name = owner.get("Name") or "desconocido"
    url = lead_url(settings.salesforce_base_url, lead_id)
    details = _lead_details(lead)

    try:
        owner_slack_id = None
        owner_email = owner.get("Email")
        if owner_email:
            logger.info("[SLACK] Buscando usuario de Slack por email=%s ...", owner_email)
            owner_slack_id = await slack.find_user_id_by_email(owner_email)
            if owner_slack_id:
                logger.info("[SLACK] Usuario encontrado: %s -> enviando DM ...", owner_slack_id)
                dm_ok = await slack.send_dm(owner_slack_id, format_owner_dm(lead_name, url, details))
                logger.info("[SLACK] DM %s a %s.", "enviado" if dm_ok else "FALLO", owner_email)
            else:
                logger.warning(
                    "[SLACK] No se encontro usuario de Slack para %s — se omite el DM "
                    "(revisar scope users:read.email, o que el email coincida con el de Slack).",
                    owner_email,
                )
        else:
            logger.warning("[SLACK] Owner sin Email — se omite el DM.")

        channel = settings.notification_channel_id
        if channel:
            logger.info("[SLACK] Posteando registro en el canal %s ...", channel)
            post_ok = await slack.post_message(
                channel,
                format_channel_fallback_text(lead_name, owner_name),
                blocks=format_channel_blocks(lead_name, url, owner_name, reason, details, owner_slack_id),
            )
            logger.info("[SLACK] Post en canal %s %s.", channel, "OK" if post_ok else "FALLO")
        else:
            logger.warning(
                "[SLACK] No hay canal configurado (LEAD_ASSIGNMENT_CHANNEL_ID / "
                "SLACK_TEST_CHANNEL_ID) — se omite el registro en Slack."
            )
    except Exception:
        logger.exception("[SLACK] Error inesperado notificando por Slack el lead %s", lead_id)


def _lead_details(lead: dict) -> dict:
    """Campos del Lead que se muestran en el mensaje de Slack (DM + canal).
    Editar aqui para agregar/quitar campos sin tocar el formateador."""
    return {
        "company": lead.get("Company"),
        "email": lead.get("Email"),
        "phone": lead.get("Phone"),
        "country": lead.get("Country_of_Residence__c"),
        "job_title": lead.get("Title"),
        "employees": lead.get("No_of_Employees__c"),
        "status": lead.get("Status"),
    }


def _lead_matches_target_segment(lead: dict) -> Optional[str]:
    """Devuelve None si el lead cumple el segmento objetivo, o el motivo del rechazo."""
    record_type_dev_name = _nested_field(lead.get("RecordType"), "DeveloperName")
    expected_record_type = lead_segment.get("record_type_developer_name")
    if expected_record_type and record_type_dev_name != expected_record_type:
        return (
            f"RecordType es '{record_type_dev_name}', se esperaba "
            f"'{expected_record_type}'."
        )

    if not _owner_is_unassigned(lead.get("Owner")):
        owner_name = _nested_name(lead.get("Owner")) or lead.get("OwnerId")
        return (
            f"El owner actual ({owner_name}) no esta en la lista de "
            f"'sin asignar de verdad' (unassigned_owners en "
            f"lead_segment.json); probablemente ya fue asignado a alguien "
            f"(o pertenece a un pais con su propia asignacion automatica que "
            f"aun no esta en esa lista)."
        )

    status = lead.get("Status")
    excluded_statuses = {_normalized(s) for s in lead_segment.get("excluded_statuses", [])}
    if _normalized(status) in excluded_statuses:
        return f"Status '{status}' esta excluido del segmento."

    expected_utm_source = lead_segment.get("utm_source")
    if expected_utm_source and lead.get("utmSource__c") != expected_utm_source:
        return (
            f"Modo prueba activo (utm_source='{expected_utm_source}' en "
            f"lead_segment.json): utmSource__c del lead es "
            f"'{lead.get('utmSource__c')}', no coincide."
        )

    return None


def _owner_is_unassigned(owner: object) -> bool:
    """True si el Owner actual del Lead es alguno de los configurados en
    lead_segment.json → unassigned_owners (el usuario de integracion para la
    mayoria de paises, o una Queue especifica para paises con su propia
    asignacion automatica, ej. Brasil)."""
    if not isinstance(owner, dict):
        return False

    owner_type = owner.get("Type")
    for entry in lead_segment.get("unassigned_owners", []):
        entry_type = (entry.get("type") or "").lower()
        identifier = entry.get("identifier")
        if not identifier:
            continue
        if entry_type == "user" and owner_type == "User" and owner.get("Username") == identifier:
            return True
        if entry_type == "queue" and owner_type == "Queue" and owner.get("Name") == identifier:
            return True

    return False


def _nested_field(value: object, field: str) -> Optional[str]:
    if isinstance(value, dict):
        return value.get(field)
    return None


def _target_title_for_country(country: Optional[str]) -> str:
    """Resuelve a que equipo (Title de Salesforce) va el lead segun su pais.

    Brasil se detecta por 'contiene' (brasil_keywords en team_routing.json),
    no por igualdad exacta, porque Country_of_Residence__c llega con datos
    inconsistentes desde la API de captura (ej. 'Brazil', 'Sao Paulo,
    Brazil', 'Brasil' del picklist) — ver team_routing.json.
    """
    normalized_country = _normalized(country)
    keywords = team_routing.get("brasil_keywords", [])
    if any(_normalized(keyword) in normalized_country for keyword in keywords if keyword):
        return team_routing["brasil_title"]
    return team_routing["default_title"]


def _validate_owner_title(owner: dict, expected_title: str) -> tuple[bool, str]:
    """Confirma que el owner encontrado realmente tiene el Title esperado.

    Es una defensa en profundidad: find_active_user_by_title ya filtra por
    Title en el WHERE, asi que esto solo protege contra un cambio de datos
    entre la busqueda y este punto, o un fallback inesperado.
    """
    owner_name = owner.get("Name") or ""
    owner_title = owner.get("Title") or _nested_name(owner.get("Profile")) or _nested_name(owner.get("UserRole"))

    if _normalized(owner_title) != _normalized(expected_title):
        return False, (
            f"El owner encontrado ({owner_name}) tiene Title '{owner_title}', "
            f"se esperaba '{expected_title}'."
        )

    return True, ""


def _nested_name(value: object) -> Optional[str]:
    if isinstance(value, dict):
        return value.get("Name")
    return None


def _normalized(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    without_accents = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(without_accents.lower().split())
