"""Shared foundations of the event protocol: the version, the clock, and the frozen body base."""

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

PROTOCOL_VERSION = 1


def now_utc() -> datetime:
    return datetime.now(UTC)


class _Body(BaseModel):
    model_config = ConfigDict(frozen=True)
