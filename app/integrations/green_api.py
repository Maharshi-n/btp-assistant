"""Thin async client for Green API (https://green-api.com)."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

import app.config as app_config

logger = logging.getLogger(__name__)


class GreenAPIError(Exception):
    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"Green API error {status_code}: {body[:200]}")
        self.status_code = status_code
        self.body = body


class GreenAPIClient:
    def __init__(self, base_url: str, instance_id: str, token: str) -> None:
        self._base = base_url.rstrip("/")
        self._instance = instance_id
        self._token = token

    def _url(self, method: str) -> str:
        return f"{self._base}/waInstance{self._instance}/{method}/{self._token}"

    async def _get(self, method: str) -> Any:
        url = self._url(method)
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.get(url)
                if resp.status_code < 500:
                    if resp.status_code >= 400:
                        raise GreenAPIError(resp.status_code, resp.text)
                    return resp.json()
                if attempt == 0:
                    await asyncio.sleep(1)
                    continue
                raise GreenAPIError(resp.status_code, resp.text)
            except GreenAPIError:
                raise
            except Exception as exc:
                if attempt == 0:
                    await asyncio.sleep(1)
                    continue
                raise GreenAPIError(0, str(exc)) from exc
        raise GreenAPIError(0, "unreachable")

    async def _post(self, method: str, body: dict) -> Any:
        url = self._url(method)
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.post(url, json=body)
                if resp.status_code < 500:
                    if resp.status_code >= 400:
                        raise GreenAPIError(resp.status_code, resp.text)
                    return resp.json()
                if attempt == 0:
                    await asyncio.sleep(1)
                    continue
                raise GreenAPIError(resp.status_code, resp.text)
            except GreenAPIError:
                raise
            except Exception as exc:
                if attempt == 0:
                    await asyncio.sleep(1)
                    continue
                raise GreenAPIError(0, str(exc)) from exc
        raise GreenAPIError(0, "unreachable")

    async def get_state(self) -> dict:
        return await self._get("getStateInstance")

    async def send_message(self, chat_id: str, text: str) -> dict:
        return await self._post("sendMessage", {"chatId": chat_id, "message": text})

    async def send_file_by_url(self, chat_id: str, url: str, filename: str, caption: str = "") -> dict:
        return await self._post("sendFileByUrl", {
            "chatId": chat_id,
            "urlFile": url,
            "fileName": filename,
            "caption": caption,
        })

    async def send_file_by_upload(
        self, chat_id: str, file_path: str, caption: str = ""
    ) -> dict:
        """Upload a local file to Green API and send it to chat_id."""
        import mimetypes
        from pathlib import Path as _Path
        p = _Path(file_path)
        mime, _ = mimetypes.guess_type(str(p))
        mime = mime or "application/octet-stream"
        url = self._url("sendFileByUpload")
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    with open(p, "rb") as fh:
                        resp = await client.post(
                            url,
                            data={"chatId": chat_id, "caption": caption},
                            files={"file": (p.name, fh, mime)},
                        )
                if resp.status_code < 500:
                    if resp.status_code >= 400:
                        raise GreenAPIError(resp.status_code, resp.text)
                    return resp.json()
                if attempt == 0:
                    await asyncio.sleep(1)
                    continue
                raise GreenAPIError(resp.status_code, resp.text)
            except GreenAPIError:
                raise
            except Exception as exc:
                if attempt == 0:
                    await asyncio.sleep(1)
                    continue
                raise GreenAPIError(0, str(exc)) from exc
        raise GreenAPIError(0, "unreachable")

    async def get_chat_history(self, chat_id: str, count: int = 50) -> list[dict]:
        result = await self._post("getChatHistory", {"chatId": chat_id, "count": count})
        return result if isinstance(result, list) else []

    async def get_contact_info(self, chat_id: str) -> dict:
        return await self._post("getContactInfo", {"chatId": chat_id})

    async def get_groups(self) -> list[dict]:
        result = await self._post("getContacts", {})
        contacts = result if isinstance(result, list) else []
        return [c for c in contacts if c.get("type") == "group"]

    async def reboot(self) -> dict:
        return await self._get("reboot")


def get_green_client() -> GreenAPIClient | None:
    if not app_config.whatsapp_enabled():
        return None
    return GreenAPIClient(
        base_url=app_config.GREEN_API_BASE_URL,
        instance_id=app_config.GREEN_API_INSTANCE_ID,
        token=app_config.GREEN_API_TOKEN,
    )
