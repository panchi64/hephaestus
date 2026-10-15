from datetime import date, datetime, time, timedelta
from typing import Self

class _Parameters(dict[str, str]): ...

class vDDDTypes:
    dt: date | datetime | time | timedelta | tuple[datetime, datetime] | tuple[datetime, timedelta]
    params: _Parameters
    def to_ical(self) -> bytes: ...

class vText(str):
    def to_ical(self) -> bytes: ...

class vDDDLists:
    dts: list[vDDDTypes]

class vRecur(dict[str, list[object]]):
    def __init__(self, *args: object, **kwargs: object) -> None: ...
    @classmethod
    def from_ical(cls, ical: str) -> Self: ...
    def to_ical(self) -> bytes: ...
