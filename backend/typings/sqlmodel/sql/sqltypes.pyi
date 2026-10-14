from datetime import datetime

from sqlalchemy import types

class AutoString(types.TypeDecorator[str]): ...

class UTCDateTime(types.TypeDecorator[datetime]):
    def __init__(self) -> None: ...
