from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

router = APIRouter()


class ConnectionManager:
    def __init__(self) -> None:
        self.active: dict[int, WebSocket] = {}  # thread_id -> websocket

    async def connect(self, thread_id: int, ws: WebSocket) -> None:
        existing = self.active.get(thread_id)
        if existing:
            try:
                await existing.close()
            except Exception:
                pass
        await ws.accept()
        self.active[thread_id] = ws
        logger.info("WebSocket connected for thread %d", thread_id)

    def disconnect(self, thread_id: int) -> None:
        self.active.pop(thread_id, None)
        logger.info("WebSocket disconnected for thread %d", thread_id)

    async def send(self, thread_id: int, data: dict) -> None:
        ws = self.active.get(thread_id)
        if ws:
            try:
                await ws.send_json(data)
            except Exception:
                self.disconnect(thread_id)


# Module-level singleton — imported by chat.py to send streamed tokens
manager = ConnectionManager()


@router.websocket("/ws/threads/{thread_id}")
async def websocket_endpoint(websocket: WebSocket, thread_id: int) -> None:
    await manager.connect(thread_id, websocket)
    try:
        # Keep the connection alive; the server pushes data, client rarely sends.
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(thread_id)
