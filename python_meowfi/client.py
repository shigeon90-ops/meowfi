from __future__ import annotations

import json
import socket
from typing import Any

try:
    from .common import PipeResponse, SERVICE_HOST, SERVICE_PORT
except ImportError:  # script mode fallback
    from common import PipeResponse, SERVICE_HOST, SERVICE_PORT


class ServiceClient:
    def __init__(self, host: str = SERVICE_HOST, port: int = SERVICE_PORT) -> None:
        self.host = host
        self.port = port

    def send(self, command: str, args: dict[str, Any] | None = None, timeout_sec: float = 8.0) -> PipeResponse:
        payload = json.dumps({"command": command, "args": args or {}}, ensure_ascii=True) + "\n"
        with socket.create_connection((self.host, self.port), timeout=timeout_sec) as sock:
            sock.settimeout(timeout_sec)
            sock.sendall(payload.encode("utf-8"))
            raw = self._readline(sock)
        if not raw:
            return PipeResponse(False, error="Empty response from service")
        return PipeResponse.from_json(raw)

    @staticmethod
    def _readline(sock: socket.socket) -> str:
        chunks: list[bytes] = []
        while True:
            data = sock.recv(4096)
            if not data:
                break
            idx = data.find(b"\n")
            if idx >= 0:
                chunks.append(data[:idx])
                break
            chunks.append(data)
        return b"".join(chunks).decode("utf-8", errors="replace").strip()
