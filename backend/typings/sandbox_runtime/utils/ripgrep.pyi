from dataclasses import dataclass

@dataclass
class RipgrepConfig:
    command: str = "rg"
    args: list[str] | None = None

def has_ripgrep_sync() -> bool: ...
