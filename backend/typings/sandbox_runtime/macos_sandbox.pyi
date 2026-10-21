from dataclasses import dataclass

from .schemas import FsReadRestrictionConfig, FsWriteRestrictionConfig

@dataclass
class MacOSSandboxParams:
    command: str
    needs_network_restriction: bool
    read_config: FsReadRestrictionConfig | None = None
    write_config: FsWriteRestrictionConfig | None = None
    allow_unix_sockets: list[str] | None = None
    allow_all_unix_sockets: bool | None = None
    ignore_violations: dict[str, list[str]] | None = None
    allow_git_config: bool = False

def wrap_command_macos(params: MacOSSandboxParams) -> str: ...
