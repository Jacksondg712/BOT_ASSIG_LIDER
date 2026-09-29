import logging
from typing import Optional

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from config import settings

logger = logging.getLogger(__name__)


class SlackService:
    """Wrapper minimo sobre AsyncWebClient para notificar asignaciones.

    Usa la app de Slack propia de este bot (ver SLACK_BOT_TOKEN en .env,
    `xoxb-...`), con Bot Token Scopes: chat:write, im:write, users:read,
    users:read.email.
    """

    def __init__(self):
        self.client = AsyncWebClient(token=settings.slack_bot_token)

    async def find_user_id_by_email(self, email: str) -> Optional[str]:
        try:
            resp = await self.client.users_lookupByEmail(email=email)
            if resp["ok"]:
                return resp["user"]["id"]
        except SlackApiError as e:
            logger.warning(
                "Slack users_lookupByEmail error para %s: %s (revisar scope users:read.email)",
                email,
                e.response["error"],
            )
        except Exception:
            logger.exception("Slack users_lookupByEmail error inesperado para %s", email)
        return None

    async def open_dm(self, user_id: str) -> Optional[str]:
        try:
            resp = await self.client.conversations_open(users=user_id)
            if resp["ok"]:
                return resp["channel"]["id"]
        except SlackApiError as e:
            logger.warning(
                "Slack conversations_open error para %s: %s (revisar scope im:write)",
                user_id,
                e.response["error"],
            )
        except Exception:
            logger.exception("Slack conversations_open error inesperado para %s", user_id)
        return None

    async def post_message(
        self,
        channel: str,
        text: str,
        blocks: Optional[list] = None,
    ) -> bool:
        try:
            kwargs: dict = {"channel": channel, "text": text}
            if blocks:
                kwargs["blocks"] = blocks
            await self.client.chat_postMessage(**kwargs)
            return True
        except SlackApiError as e:
            logger.warning(
                "Slack chat_postMessage error para canal %s: %s (revisar scope chat:write)",
                channel,
                e.response["error"],
            )
        except Exception:
            logger.exception("Slack chat_postMessage error inesperado para canal %s", channel)
        return False

    async def send_dm(self, user_id: str, text: str, blocks: Optional[list] = None) -> bool:
        dm_channel = await self.open_dm(user_id)
        if not dm_channel:
            return False
        return await self.post_message(dm_channel, text, blocks)
