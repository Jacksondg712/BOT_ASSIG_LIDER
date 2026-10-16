import logging
import unicodedata
from typing import List, Optional

from config import lead_segment, settings, team_routing
from models.schemas import LeadAssignmentResult
from services.attendance import filter_present as _filter_present
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


def was_processed(lead_id: str) -> bool:
    """True si el lead ya se proceso sin error en este proceso — en ese caso
    handle_lead_ids lo omite y NO devuelve resultado para el."""
    return lead_id in _PROCESSED_LEAD_IDS


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
    target_titles = _target_titles_for_country(country)
    team_label = " + ".join(target_titles)
    candidates = await salesforce.find_active_users_by_titles(target_titles)
    if not candidates:
        return LeadAssignmentResult(
            lead_id=lead_id,
            lead_name=lead_name,
            action="error",
            reason=(
                f"No encontre un usuario activo en Salesforce con Title "
                f"'{team_label}' (equipo resuelto para el pais '{country}'). "
                "Verifica que ese equipo tenga al menos una persona activa "
                "con alguno de esos Titles en team_routing.json."
            ),
        )

    # Si hay mas de un owner activo en el pool (que puede juntar varios
    # Titles, ej. Latam = (LT) + (LT) (US)), se reparte equitativo
    # (round-robin) en vez de siempre caer en el mismo — ver
    # services/round_robin.py. Con un solo candidato, devuelve siempre ese.
    # La rotacion se lleva por pool (clave = team_label), no por Title suelto.
    #
    # Nota: el puntero de rotacion avanza aqui, al elegir, no recien cuando
    # se confirma el PATCH en Salesforce. Si algo falla despues (Title
    # invalido, error de Salesforce), este intento "gasto su turno" en la
    # rotacion igual — se acepto este trade-off por simplicidad, ya que esos
    # fallos deberian ser raros/transitorios y el lead se reintenta solo en
    # el siguiente ciclo (con el siguiente owner en turno).
    #
    # Antes del reparto, en pools de 2+ personas se descartan quienes no
    # estan conectados hoy segun la asistencia (LoginHistory, ver
    # services/attendance.py). Con 1 sola persona no se filtra nunca.
    attendance = await _filter_present(candidates, salesforce)
    owner = _pick_next_owner(team_label, attendance.candidates)

    valid, reason = _validate_owner_title(owner, target_titles)
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
        f"Asignado a {owner_name} (equipo '{team_label}', pais '{country}'). "
        f"Titulo verificado."
    )
    if attendance.note:
        success_reason += f" {attendance.note}"
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

    quality_reason = _quality_filter_rejection_reason(lead)
    if quality_reason:
        return quality_reason

    return None


def _quality_filter_rejection_reason(lead: dict) -> Optional[str]:
    """Replica las exclusiones de calidad de dato del reporte de produccion
    original (emails/companies de prueba, paises restringidos, trafico de
    eventos, leads con campana propia) — ver quality_filters en
    lead_segment.json. Devuelve None si el lead pasa todos los filtros."""
    filters = lead_segment.get("quality_filters", {})
    if not filters:
        return None

    # Solo Email, NO Company: un nombre/empresa real puede contener "test"
    # como substring sin ser de prueba (ej. "Alfredo Testa", "Contesta SAS")
    # — se encontro este falso positivo probando en seco antes de ir a
    # produccion (2026-09-30). El email SI es una senal confiable (emails
    # internos tipo "+test@" o "@openenglish.com").
    email_excluded = filters.get("email_company_excluded_keywords", [])
    if _contains_any(lead.get("Email"), email_excluded):
        return (
            "Excluido por filtro de calidad: Email contiene una "
            f"palabra de prueba/interna ({email_excluded})."
        )

    excluded_countries = filters.get("excluded_country_keywords", [])
    if _contains_any(lead.get("Country_of_Residence__c"), excluded_countries):
        return (
            f"Excluido por filtro de calidad: pais restringido "
            f"('{lead.get('Country_of_Residence__c')}')."
        )

    utm_excluded = filters.get("utm_source_excluded_keywords", [])
    if _contains_any(lead.get("utmSource__c"), utm_excluded):
        return (
            f"Excluido por filtro de calidad: utmSource__c "
            f"('{lead.get('utmSource__c')}') es trafico de campana/evento."
        )

    landing_page = lead.get("landingPagesCurrentIndex__c")
    required_landing_keywords = filters.get("landing_page_required_keywords", [])
    if required_landing_keywords and not _contains_any(landing_page, required_landing_keywords):
        return (
            f"Excluido por filtro de calidad: landingPagesCurrentIndex__c "
            f"('{landing_page}') no es una landing page de tipo para-empresas."
        )

    excluded_landing_keywords = filters.get("landing_page_excluded_keywords", [])
    if _contains_any(landing_page, excluded_landing_keywords):
        return (
            f"Excluido por filtro de calidad: landingPagesCurrentIndex__c "
            f"('{landing_page}') esta en la lista de landing pages excluidas."
        )

    if filters.get("campaign_field_must_be_empty") and lead.get("LR_Lead_Campaign__c"):
        return (
            "Excluido por filtro de calidad: LR_Lead_Campaign__c no esta "
            f"vacio ('{lead.get('LR_Lead_Campaign__c')}') — ese lead tiene su "
            "propio flujo de asignacion por campana."
        )

    return None


def _contains_any(value: object, keywords: list) -> bool:
    if not keywords:
        return False
    normalized_value = _normalized(value)
    if not normalized_value:
        return False
    return any(_normalized(keyword) in normalized_value for keyword in keywords if keyword)


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


def _configured_titles(plural_key: str, singular_key: str) -> list:
    """Lista de Titles de una ruta en team_routing.json. Acepta la clave en
    plural (lista) y, por compatibilidad con configs viejas, la singular."""
    titles = team_routing.get(plural_key)
    if titles:
        return list(titles)
    single = team_routing.get(singular_key)
    return [single] if single else []


def _target_titles_for_country(country: Optional[str]) -> list:
    """Resuelve que Titles de Salesforce trabajan el lead segun su pais. El
    owner se elige (round-robin) entre TODOS los usuarios activos que tengan
    cualquiera de esos Titles.

    - Brasil -> brasil_titles.
    - USA    -> usa_titles (solo (LT) (US)).
    - Resto de Latam -> default_titles ((LT) + (LT) (US) juntos).

    Brasil y USA se detectan por 'contiene' (brasil_keywords / usa_keywords
    en team_routing.json), no por igualdad exacta, porque
    Country_of_Residence__c llega con datos inconsistentes desde la API de
    captura (ej. 'Brazil', 'Sao Paulo, Brazil', 'Brasil' del picklist; o
    'USA', 'United States', 'Estados Unidos de America') — ver
    team_routing.json.
    """
    normalized_country = _normalized(country)

    brasil_keywords = team_routing.get("brasil_keywords", [])
    if any(_normalized(keyword) in normalized_country for keyword in brasil_keywords if keyword):
        return _configured_titles("brasil_titles", "brasil_title")

    usa_keywords = team_routing.get("usa_keywords", [])
    if usa_keywords and any(_normalized(keyword) in normalized_country for keyword in usa_keywords if keyword):
        usa_titles = _configured_titles("usa_titles", "usa_title")
        if usa_titles:
            return usa_titles

    return _configured_titles("default_titles", "default_title")


def _validate_owner_title(owner: dict, expected_titles) -> tuple[bool, str]:
    """Confirma que el owner encontrado realmente tiene alguno de los Titles
    esperados (acepta un solo Title o una lista).

    Es una defensa en profundidad: find_active_users_by_titles ya filtra por
    Title en el WHERE, asi que esto solo protege contra un cambio de datos
    entre la busqueda y este punto, o un fallback inesperado.
    """
    if isinstance(expected_titles, str):
        expected_titles = [expected_titles]

    owner_name = owner.get("Name") or ""
    owner_title = owner.get("Title") or _nested_name(owner.get("Profile")) or _nested_name(owner.get("UserRole"))

    if _normalized(owner_title) not in {_normalized(title) for title in expected_titles}:
        return False, (
            f"El owner encontrado ({owner_name}) tiene Title '{owner_title}', "
            f"se esperaba alguno de: {', '.join(expected_titles)}."
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
