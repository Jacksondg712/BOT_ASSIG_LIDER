import json as _json
from pathlib import Path

from pydantic_settings import BaseSettings

# Enrutamiento del owner por pais (Country_of_Residence__c del Lead) —
# editar team_routing.json para agregar equipos/paises sin tocar codigo.
_routing_path = Path(__file__).with_name("team_routing.json")
team_routing: dict = _json.loads(_routing_path.read_text(encoding="utf-8"))

# Criterio del segmento objetivo (equivalente al reporte "Copia de
# Leads-Created-B2B_All-Unassigne") — editar lead_segment.json para ajustar
# sin tocar codigo.
_segment_path = Path(__file__).with_name("lead_segment.json")
lead_segment: dict = _json.loads(_segment_path.read_text(encoding="utf-8"))


class Settings(BaseSettings):
    # ------------------------------------------------------------------ #
    #  SECRETOS — deben estar en variables de entorno (.env)              #
    # ------------------------------------------------------------------ #
    # Salesforce: usar OAuth (client_id + client_secret) O SOAP (username + password + token)
    salesforce_client_id: str = ""
    salesforce_client_secret: str = ""
    salesforce_username: str = ""
    salesforce_password: str = ""
    salesforce_security_token: str = ""

    salesforce_base_url: str = "https://your-instance.my.salesforce.com"
    salesforce_login_url: str = "https://login.salesforce.com"
    salesforce_api_version: str = "v61.0"

    # Token compartido que Salesforce debe mandar (query param `token` o
    # header `X-Webhook-Token`) para que el webhook acepte la notificacion.
    # Se deja armado para cuando se pueda configurar el Flow (requiere permisos
    # de admin que hoy no estan disponibles); mientras tanto el bot usa polling.
    lead_webhook_token: str = ""

    # --- Polling: el bot consulta Salesforce cada N segundos en vez de ---
    # --- esperar una notificacion (Flow/Outbound Message). ---
    poll_enabled: bool = True
    poll_interval_seconds: int = 60
    poll_batch_size: int = 20

    # --- Slack: notificacion de asignaciones (DM al owner + registro en ---
    # --- un canal). App propia de este bot (Bot Token Scopes: chat:write, ---
    # --- im:write, users:read, users:read.email). ---
    slack_bot_token: str = ""
    slack_notifications_enabled: bool = True
    # Canal donde se postea el registro de asignaciones. Si no se configura,
    # cae a slack_test_channel_id (channel donde ya esta invitado el bot)
    # para no bloquear el arranque.
    lead_assignment_channel_id: str = ""
    slack_test_channel_id: str = ""

    # Silencia los logs de linea-por-linea de httpx/httpcore (cada request a
    # Salesforce) para que los logs de [SLACK] no queden enterrados.
    slack_test_mode: bool = False

    class Config:
        env_file = Path(__file__).with_name(".env")
        extra = "ignore"

    @property
    def notification_channel_id(self) -> str:
        return self.lead_assignment_channel_id or self.slack_test_channel_id


settings = Settings()
