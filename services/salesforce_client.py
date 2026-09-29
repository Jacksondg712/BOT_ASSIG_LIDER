import logging
from dataclasses import dataclass
from html import escape
from typing import Any, Iterable, Optional
from xml.etree import ElementTree as ET

import httpx

from config import settings

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(15.0)


class SalesforceAuthError(RuntimeError):
    pass


class SalesforceApiError(RuntimeError):
    pass


@dataclass
class SalesforceSession:
    access_token: str
    instance_url: str


def _soql_quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _csv_quoted(values: Iterable[str]) -> str:
    return ", ".join(_soql_quote(value) for value in values)


class SalesforceClient:
    """Cliente minimo: solo lo necesario para el flujo de asignacion de Leads.

    Reutiliza el mismo patron de login (OAuth password flow o SOAP) que
    BOT_SLACK_SALESFORCE/services/salesforce_client.py.
    """

    def __init__(self):
        self._base = settings.salesforce_base_url.rstrip("/")
        self._login_url = settings.salesforce_login_url.rstrip("/") or self._base
        self._api_version = settings.salesforce_api_version
        self._session: Optional[SalesforceSession] = None
        self._record_type_ids: dict[str, str] = {}
        self._user_ids_by_username: dict[str, str] = {}
        self._queue_ids_by_name: dict[str, str] = {}

    async def get_lead(self, lead_id: str) -> Optional[dict[str, Any]]:
        logger.info("SF get_lead: %s", lead_id)
        try:
            records = await self.query_all(
                "SELECT Id, Name, OwnerId, Owner.Name, Owner.Username, Owner.Type, "
                "Status, CreatedDate, RecordTypeId, RecordType.Name, "
                "RecordType.DeveloperName, Company, Email, Phone, "
                "Country_of_Residence__c, Title, No_of_Employees__c, utmSource__c "
                "FROM Lead "
                f"WHERE Id = {_soql_quote(lead_id)} "
                "LIMIT 1"
            )
        except Exception:
            logger.exception("SF get_lead error: %s", lead_id)
            return None
        return records[0] if records else None

    async def find_active_user(self, email: Optional[str] = None) -> Optional[dict[str, Any]]:
        logger.info("SF find_active_user: email=%s", email)
        if not email:
            return None

        records = await self.query_all(
            "SELECT Id, Name, Email, IsActive, Title, Profile.Name, UserRole.Name "
            "FROM User "
            f"WHERE IsActive = true AND Email = {_soql_quote(email)} "
            "ORDER BY LastModifiedDate DESC "
            "LIMIT 1"
        )
        return records[0] if records else None

    async def find_active_user_by_title(self, title: Optional[str]) -> Optional[dict[str, Any]]:
        logger.info("SF find_active_user_by_title: title=%s", title)
        if not title:
            return None

        records = await self.query_all(
            "SELECT Id, Name, Email, IsActive, Title, Profile.Name, UserRole.Name "
            "FROM User "
            f"WHERE IsActive = true AND Title = {_soql_quote(title)} "
            "ORDER BY LastModifiedDate DESC "
            "LIMIT 1"
        )
        return records[0] if records else None

    async def get_record_type_id(self, developer_name: str) -> Optional[str]:
        if developer_name in self._record_type_ids:
            return self._record_type_ids[developer_name]

        records = await self.query_all(
            "SELECT Id FROM RecordType "
            f"WHERE SobjectType = 'Lead' AND DeveloperName = {_soql_quote(developer_name)} "
            "LIMIT 1"
        )
        if not records:
            logger.warning("RecordType de Lead no encontrado: %s", developer_name)
            return None

        record_type_id = records[0]["Id"]
        self._record_type_ids[developer_name] = record_type_id
        return record_type_id

    async def get_user_id_by_username(self, username: str) -> Optional[str]:
        if username in self._user_ids_by_username:
            return self._user_ids_by_username[username]

        records = await self.query_all(
            "SELECT Id FROM User "
            f"WHERE Username = {_soql_quote(username)} "
            "LIMIT 1"
        )
        if not records:
            logger.warning("User no encontrado por username: %s", username)
            return None

        user_id = records[0]["Id"]
        self._user_ids_by_username[username] = user_id
        return user_id

    async def get_queue_id_by_name(self, name: str) -> Optional[str]:
        """Resuelve el Id de una Queue (Group con Type='Queue') por su Name.

        Necesario porque algunos paises (ej. Brasil) tienen una asignacion
        automatica propia que manda el lead directo a una Queue en vez de
        dejarlo con el usuario de integracion — ver lead_segment.json.
        """
        if name in self._queue_ids_by_name:
            return self._queue_ids_by_name[name]

        records = await self.query_all(
            "SELECT Id FROM Group "
            f"WHERE Type = 'Queue' AND Name = {_soql_quote(name)} "
            "LIMIT 1"
        )
        if not records:
            logger.warning("Queue no encontrada por nombre: %s", name)
            return None

        queue_id = records[0]["Id"]
        self._queue_ids_by_name[name] = queue_id
        return queue_id

    async def find_unassigned_lead_ids(
        self,
        record_type_id: str,
        owner_ids: Iterable[str],
        excluded_statuses: Iterable[str],
        since_created_date: Optional[str] = None,
        limit: int = 20,
        utm_source: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Busca Leads que hoy cumplen el segmento objetivo (para polling).

        Devuelve Id + CreatedDate, ordenados por CreatedDate ascendente, para
        que el poller pueda avanzar su checkpoint de forma incremental.

        `owner_ids`: TODOS los Ids (usuario de integracion, Queues especificas
        de ciertos paises, etc.) que cuentan como "sin asignar de verdad" —
        ver lead_segment.json → unassigned_owners.

        `utm_source`: si se pasa (modo prueba), restringe a
        utmSource__c = <valor> — usado para aislar los leads del reporte de
        pruebas y no tocar trafico real mientras se valida el bot.
        """
        owner_ids_list = [owner_id for owner_id in owner_ids if owner_id]
        if not owner_ids_list:
            logger.error("find_unassigned_lead_ids: no hay owner_ids validos para buscar.")
            return []

        clauses = [
            f"RecordTypeId = {_soql_quote(record_type_id)}",
            f"OwnerId IN ({_csv_quoted(owner_ids_list)})",
        ]
        excluded = [status for status in excluded_statuses if status]
        if excluded:
            clauses.append(f"Status NOT IN ({_csv_quoted(excluded)})")
        if since_created_date:
            clauses.append(f"CreatedDate > {since_created_date}")
        if utm_source:
            clauses.append(f"utmSource__c = {_soql_quote(utm_source)}")

        soql = (
            "SELECT Id, CreatedDate FROM Lead "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY CreatedDate ASC "
            f"LIMIT {int(limit)}"
        )
        logger.info("SF find_unassigned_lead_ids: %s", soql)
        return await self.query_all(soql)

    async def update_lead_owner(self, lead_id: str, owner_id: str) -> bool:
        logger.info("SF update_lead_owner: %s -> owner=%s", lead_id, owner_id)
        response = await self._request(
            "PATCH",
            f"/services/data/{self._api_version}/sobjects/Lead/{lead_id}",
            json={"OwnerId": owner_id},
        )
        return response.status_code == 204

    async def query_all(self, soql: str) -> list[dict[str, Any]]:
        response = await self._request(
            "GET",
            f"/services/data/{self._api_version}/query",
            params={"q": soql},
        )
        payload = response.json()
        records = payload.get("records", [])

        next_url = payload.get("nextRecordsUrl")
        while next_url:
            response = await self._request("GET", next_url)
            payload = response.json()
            records.extend(payload.get("records", []))
            next_url = payload.get("nextRecordsUrl")

        return records

    async def list_lead_listviews(self) -> list[dict[str, Any]]:
        """Utilidad de diagnostico: lista los List Views de Lead disponibles."""
        response = await self._request(
            "GET",
            f"/services/data/{self._api_version}/sobjects/Lead/listviews",
        )
        return response.json().get("listviews", [])

    async def describe_lead_listview(self, listview_id: str) -> dict[str, Any]:
        """Utilidad de diagnostico: trae el criterio de filtro de un List View."""
        response = await self._request(
            "GET",
            f"/services/data/{self._api_version}/sobjects/Lead/listviews/{listview_id}/describe",
        )
        return response.json()

    async def test_connection(self) -> bool:
        await self._ensure_session()
        return True

    async def _ensure_session(self) -> SalesforceSession:
        if self._session:
            return self._session

        if settings.salesforce_client_id and settings.salesforce_client_secret:
            self._session = await self._login_with_oauth_password_flow()
        else:
            self._session = await self._login_with_soap()

        return self._session

    async def _login_with_oauth_password_flow(self) -> SalesforceSession:
        if not settings.salesforce_username or not settings.salesforce_password:
            raise SalesforceAuthError("Faltan credenciales Salesforce.")

        password = settings.salesforce_password + settings.salesforce_security_token
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                f"{self._login_url}/services/oauth2/token",
                data={
                    "grant_type": "password",
                    "client_id": settings.salesforce_client_id,
                    "client_secret": settings.salesforce_client_secret,
                    "username": settings.salesforce_username,
                    "password": password,
                },
            )

        if response.status_code >= 400:
            logger.error("OAuth Salesforce fallo con status=%s", response.status_code)
            raise SalesforceAuthError("No se pudo autenticar con Salesforce OAuth.")

        payload = response.json()
        return SalesforceSession(
            access_token=payload["access_token"],
            instance_url=payload.get("instance_url", self._base).rstrip("/"),
        )

    async def _login_with_soap(self) -> SalesforceSession:
        if not settings.salesforce_username or not settings.salesforce_password:
            raise SalesforceAuthError("Faltan credenciales Salesforce.")

        password = settings.salesforce_password + settings.salesforce_security_token
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<env:Envelope xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xmlns:env="http://schemas.xmlsoap.org/soap/envelope/">'
            "<env:Body>"
            '<n1:login xmlns:n1="urn:partner.soap.sforce.com">'
            f"<n1:username>{escape(settings.salesforce_username)}</n1:username>"
            f"<n1:password>{escape(password)}</n1:password>"
            "</n1:login>"
            "</env:Body>"
            "</env:Envelope>"
        )

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                f"{self._login_url}/services/Soap/u/{self._api_version}",
                content=body,
                headers={
                    "Content-Type": "text/xml; charset=UTF-8",
                    "SOAPAction": "login",
                },
            )

        if response.status_code >= 400:
            logger.error("SOAP Salesforce fallo con status=%s", response.status_code)
            raise SalesforceAuthError("No se pudo autenticar con Salesforce SOAP.")

        root = ET.fromstring(response.text)
        session_id = self._find_xml_text(root, "sessionId")
        server_url = self._find_xml_text(root, "serverUrl")
        if not session_id or not server_url:
            fault = self._find_xml_text(root, "faultstring") or "Respuesta SOAP invalida."
            logger.error("SOAP Salesforce sin sessionId/serverUrl: %s", fault)
            raise SalesforceAuthError(f"No se pudo autenticar con Salesforce: {fault}")

        instance_url = server_url.split("/services/Soap/")[0].rstrip("/")
        return SalesforceSession(access_token=session_id, instance_url=instance_url)

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        session = await self._ensure_session()
        url = path if path.startswith("http") else f"{session.instance_url}{path}"
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {session.access_token}"

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.request(method, url, headers=headers, **kwargs)

        if response.status_code == 401:
            self._session = None
            session = await self._ensure_session()
            headers["Authorization"] = f"Bearer {session.access_token}"
            url = path if path.startswith("http") else f"{session.instance_url}{path}"
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.request(method, url, headers=headers, **kwargs)

        if response.status_code >= 400:
            logger.error(
                "Salesforce API error status=%s body=%s",
                response.status_code,
                response.text[:500],
            )
            raise SalesforceApiError(f"Salesforce API error {response.status_code}")

        return response

    @staticmethod
    def _find_xml_text(root: ET.Element, tag_name: str) -> Optional[str]:
        for element in root.iter():
            if element.tag.endswith(tag_name):
                return element.text
        return None
