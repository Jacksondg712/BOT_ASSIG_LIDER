import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from pydantic import BaseModel

from config import attendance_config, settings
from services.attendance_report import AttendanceReportLoop
from services.lead_assignment import _notify_assignment, handle_lead_ids, salesforce, slack
from services.poller import PollLoop, load_checkpoint, run_poll_cycle
from utils.payload_parser import build_outbound_message_ack, parse_notification

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Mientras se prueba Slack, el ruido linea-por-linea de httpx (cada request a
# Salesforce/Slack) tapa lo importante. Se silencia a WARNING; nuestros logs
# propios (services.*) se quedan en INFO igual. Poner SLACK_TEST_MODE=false
# en .env cuando ya no se necesite este modo enfocado.
if settings.slack_test_mode:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

_poll_loop = PollLoop()
_attendance_report_loop = AttendanceReportLoop(salesforce, slack)


@asynccontextmanager
async def lifespan(app: FastAPI):
    poll_task = None
    if settings.poll_enabled:
        poll_task = asyncio.create_task(_poll_loop.run())

    # Mensaje de asistencia (lunes a viernes habiles, ver attendance.json).
    report_task = None
    if attendance_config.get("enabled") and attendance_config.get("report", {}).get("enabled"):
        report_task = asyncio.create_task(_attendance_report_loop.run())

    yield
    if poll_task:
        _poll_loop.stop()
        poll_task.cancel()
    if report_task:
        _attendance_report_loop.stop()
        report_task.cancel()


app = FastAPI(
    title="BOT_ASSIG_LIDER",
    description=(
        "Asigna automaticamente el owner de Leads nuevos del segmento "
        "'Copia de Leads-Created-B2B_All-Unassigne' a un owner con titulo "
        "de lider permitido. Modo activo: polling periodico (ver "
        "POLL_INTERVAL_SECONDS). El webhook /salesforce/lead-created queda "
        "listo para cuando se pueda configurar un Flow en Salesforce."
    ),
    version="0.2.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "poll_enabled": settings.poll_enabled,
        "poll_interval_seconds": settings.poll_interval_seconds,
        "last_checkpoint": load_checkpoint(),
    }


@app.post("/poll/run-once")
async def poll_run_once():
    """Dispara un ciclo de polling manualmente, sin esperar el intervalo.

    Util para probar de punta a punta sin quedarse esperando.
    """
    results = await run_poll_cycle()
    return {"ok": True, "processed": [r.model_dump() for r in results]}


class SlackTestNotifyRequest(BaseModel):
    owner_email: str
    owner_name: str = "Owner de prueba"
    lead_id: str = "00Q000000000000AAA"
    lead_name: str = "Lead de prueba"
    company: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    country: Optional[str] = None
    job_title: Optional[str] = None
    employees: Optional[str] = None
    status: Optional[str] = "Prospect"


@app.post("/slack/test-notify")
async def slack_test_notify(payload: SlackTestNotifyRequest):
    """Envia un DM de prueba al owner + un post de prueba al canal, con el
    mismo texto predeterminado que usaria una asignacion real. NO toca
    Salesforce (todos los campos del lead son de mentira, solo para armar el
    link y el texto). Util para validar de punta a punta que el token de
    Slack tiene los scopes necesarios (chat:write, im:write,
    users:read.email) antes de depender de una asignacion real.
    """
    owner = {"Name": payload.owner_name, "Email": payload.owner_email}
    lead = {
        "Id": payload.lead_id,
        "Name": payload.lead_name,
        "Company": payload.company,
        "Email": payload.email,
        "Phone": payload.phone,
        "Country_of_Residence__c": payload.country,
        "Title": payload.job_title,
        "No_of_Employees__c": payload.employees,
        "Status": payload.status,
    }
    await _notify_assignment(
        lead=lead,
        owner=owner,
        reason="Mensaje de prueba manual — no corresponde a una asignacion real.",
    )
    return {
        "ok": True,
        "notification_channel_id": settings.notification_channel_id,
        "note": "Revisa los logs del servidor para ver si Slack acepto el DM y el post del canal, o si falto algun scope.",
    }


@app.post("/salesforce/lead-created")
async def lead_created(request: Request, background_tasks: BackgroundTasks):
    _check_webhook_token(request)

    body_bytes = await request.body()
    content_type = request.headers.get("content-type", "")
    notification = parse_notification(body_bytes, content_type)

    logger.info(
        "Notificacion recibida: is_outbound_message=%s lead_ids=%s",
        notification.is_outbound_message,
        notification.lead_ids,
    )

    if notification.lead_ids:
        background_tasks.add_task(handle_lead_ids, notification.lead_ids)
    else:
        logger.warning("Notificacion sin lead_ids reconocibles: %r", body_bytes[:500])

    if notification.is_outbound_message:
        # Salesforce (Outbound Message / Workflow Rule) espera este Ack SOAP;
        # si no lo recibe, reintenta la notificacion.
        return PlainTextResponse(
            build_outbound_message_ack(),
            media_type="text/xml",
        )

    return {"ok": True, "lead_ids": notification.lead_ids}


def _check_webhook_token(request: Request) -> None:
    if not settings.lead_webhook_token:
        # Sin token configurado, no se valida (solo para desarrollo local).
        logger.warning("LEAD_WEBHOOK_TOKEN no configurado — el endpoint acepta cualquier llamada.")
        return

    provided = request.query_params.get("token") or request.headers.get("x-webhook-token")
    if provided != settings.lead_webhook_token:
        logger.warning("Llamada al webhook rechazada: token invalido o ausente.")
        raise HTTPException(status_code=403, detail="Invalid webhook token")
