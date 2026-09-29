"""Extrae el Id del Lead desde la notificacion que manda Salesforce.

Se soportan dos formatos, para no atar el bot a un unico mecanismo de disparo
del lado de Salesforce:

1. JSON simple — de un Flow con la accion "HTTP Callout" (recomendado, no
   requiere Apex), o de un Apex callout propio:
       {"lead_id": "00Q..."}
   tambien se aceptan las variantes "leadId" / "id" / "Id".

2. Outbound Message (SOAP XML) — de un Workflow Rule con accion
   "Outbound Message". Es la opcion mas simple de configurar sin escribir
   Apex, aunque Salesforce la considera legacy. Salesforce manda un XML como:
       <soapenv:Envelope ...>
         <soapenv:Body>
           <notifications ...>
             <Notification>
               <Id>...</Id>
               <sObject xsi:type="sf:Lead">
                 <sf:Id>00Q...</sf:Id>
               </sObject>
             </Notification>
           </notifications>
         </soapenv:Body>
       </soapenv:Envelope>
   En ese caso hay que responder con el Ack SOAP esperado (ver
   `build_outbound_message_ack`), si no Salesforce reintenta la notificacion.
"""

from dataclasses import dataclass
from typing import Optional
from xml.etree import ElementTree as ET

_LEAD_ID_PREFIX = "00Q"


@dataclass
class ParsedNotification:
    lead_ids: list[str]
    is_outbound_message: bool
    ack_id: Optional[str] = None  # solo para Outbound Message


def parse_notification(body_bytes: bytes, content_type: str) -> ParsedNotification:
    content_type = (content_type or "").lower()

    if "xml" in content_type or body_bytes.lstrip().startswith(b"<"):
        return _parse_outbound_message(body_bytes)

    return _parse_json(body_bytes)


def _parse_json(body_bytes: bytes) -> ParsedNotification:
    import json

    try:
        payload = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return ParsedNotification(lead_ids=[], is_outbound_message=False)

    lead_ids: list[str] = []

    def _collect(value):
        if isinstance(value, str) and value.startswith(_LEAD_ID_PREFIX):
            lead_ids.append(value)
        elif isinstance(value, dict):
            for key in ("lead_id", "leadId", "LeadId", "id", "Id", "recordId", "RecordId"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.startswith(_LEAD_ID_PREFIX):
                    lead_ids.append(candidate)
            for child in value.values():
                _collect(child)
        elif isinstance(value, list):
            for item in value:
                _collect(item)

    _collect(payload)
    # dedupe preservando orden
    seen = set()
    unique_ids = []
    for lead_id in lead_ids:
        if lead_id not in seen:
            seen.add(lead_id)
            unique_ids.append(lead_id)

    return ParsedNotification(lead_ids=unique_ids, is_outbound_message=False)


def _parse_outbound_message(body_bytes: bytes) -> ParsedNotification:
    try:
        root = ET.fromstring(body_bytes)
    except ET.ParseError:
        return ParsedNotification(lead_ids=[], is_outbound_message=True)

    lead_ids: list[str] = []
    ack_id: Optional[str] = None

    for element in root.iter():
        tag = element.tag.split("}")[-1]
        if tag == "OrganizationId" and ack_id is None:
            # No es el Id que necesitamos, pero confirma que es un Outbound Message.
            pass
        if tag == "Id" and element.text and element.text.startswith(_LEAD_ID_PREFIX):
            lead_ids.append(element.text)

    seen = set()
    unique_ids = []
    for lead_id in lead_ids:
        if lead_id not in seen:
            seen.add(lead_id)
            unique_ids.append(lead_id)

    return ParsedNotification(lead_ids=unique_ids, is_outbound_message=True, ack_id=ack_id)


def build_outbound_message_ack() -> str:
    """Respuesta SOAP que Salesforce espera para dar por entregado el Outbound Message."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
        "<soapenv:Body>"
        '<notificationsResponse xmlns="http://soap.sforce.com/2005/09/outbound">'
        "<Ack>true</Ack>"
        "</notificationsResponse>"
        "</soapenv:Body>"
        "</soapenv:Envelope>"
    )
