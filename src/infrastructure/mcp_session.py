"""
MCP 2024-11-05 Session Lifecycle Manager.
Tracks client session identifiers, queues and activity timestamps.
"""
import asyncio
import time
import uuid
from typing import Dict, Optional


class MCPSession:
    def __init__(self, session_id: str, auth_header: Optional[str] = None):
        self.session_id = session_id
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self.auth_header = auth_header
        self.created_at: float = time.time()
        self.last_active: float = time.time()


class MCPSessionManager:
    def __init__(self):
        self._sessions: Dict[str, MCPSession] = {}
        self._lock = asyncio.Lock()

    async def create_session(self, auth_header: Optional[str] = None) -> MCPSession:
        async with self._lock:
            sid = uuid.uuid4().hex
            session = MCPSession(session_id=sid, auth_header=auth_header)
            self._sessions[sid] = session
            return session

    async def get_session(self, session_id: str) -> Optional[MCPSession]:
        async with self._lock:
            s = self._sessions.get(session_id)
            if s:
                s.last_active = time.time()
            return s

    async def remove_session(self, session_id: str):
        async with self._lock:
            self._sessions.pop(session_id, None)

    async def get_or_create(self, session_id: Optional[str] = None, auth_header: Optional[str] = None) -> MCPSession:
        if session_id:
            s = await self.get_session(session_id)
            if s:
                return s
        return await self.create_session(auth_header=auth_header)
