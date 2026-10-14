class SmolVMError(Exception):
    message: str
    details: dict[object, object]
    def __init__(self, message: str, details: dict[object, object] | None = None) -> None: ...
