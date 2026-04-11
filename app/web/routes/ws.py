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

    # Re-send any pending permission requests for this thread so the UI
    # can show the approval card even after a page refresh or reconnect.
    try:
        from app.web.routes.chat import get_pending_permissions
        for req_id, req in get_pending_permissions().items():
            if req["thread_id"] == thread_id:
                await websocket.send_json({
                    "type": "permission_request",
                    "id": req_id,
                    "tool": req["tool"],
                    "args": req["args"],
                    "prompt": req["prompt"],
                })
    except Exception:
        pass  # never crash the WS connection over this

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(thread_id)
