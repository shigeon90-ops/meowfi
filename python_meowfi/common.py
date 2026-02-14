from __future__ import annotations

import dataclasses
import datetime as dt
import json
from typing import Any


APP_NAME = "MeowFi"
SERVICE_HOST = "127.0.0.1"
SERVICE_PORT = 38777
SERVICE_VERSION = "1.0.0-py"


@dataclasses.dataclass(slots=True)
class PipeRequest:
    command: str
    args: dict[str, str | None] | None = None


@dataclasses.dataclass(slots=True)
class PipeResponse:
    success: bool
    error: str | None = None
    version: str | None = None
    state: str | None = None
    message: str | None = None
    data: Any = None

    def to_json_line(self) -> str:
        return json.dumps(dataclasses.asdict(self), ensure_ascii=True) + "\n"

    @staticmethod
    def from_json(raw: str) -> "PipeResponse":
        data = json.loads(raw)
        return PipeResponse(
            success=bool(data.get("success", False)),
            error=data.get("error"),
            version=data.get("version"),
            state=data.get("state"),
            message=data.get("message"),
            data=data.get("data"),
        )


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()