"""WebSocket connection manager for real-time features."""

from typing import Dict, Set
from uuid import UUID
from fastapi import WebSocket
import json


class ConnectionManager:
    """Manages WebSocket connections per user."""

    def __init__(self):
        self.active_connections: Dict[str, Set[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        if user_id not in self.active_connections:
            self.active_connections[user_id] = set()
        self.active_connections[user_id].add(websocket)

    def disconnect(self, websocket: WebSocket, user_id: str):
        if user_id in self.active_connections:
            self.active_connections[user_id].discard(websocket)
            if not self.active_connections[user_id]:
                del self.active_connections[user_id]

    async def send_to_user(self, user_id: str, message: dict):
        """Send message to all connections of a user."""
        if user_id in self.active_connections:
            data = json.dumps(message)
            for ws in self.active_connections[user_id].copy():
                try:
                    await ws.send_text(data)
                except Exception:
                    self.active_connections[user_id].discard(ws)

    async def broadcast(self, message: dict, exclude: str = None):
        """Broadcast to all connected users."""
        data = json.dumps(message)
        for user_id, connections in self.active_connections.items():
            if user_id == exclude:
                continue
            for ws in connections.copy():
                try:
                    await ws.send_text(data)
                except Exception:
                    connections.discard(ws)

    def is_online(self, user_id: str) -> bool:
        return user_id in self.active_connections and len(self.active_connections[user_id]) > 0


# Singleton
ws_manager = ConnectionManager()
