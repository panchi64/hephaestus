from dataclasses import dataclass

from .schemas import FsReadRestrictionConfig, FsWriteRestrictionConfig
from .utils.ripgrep import RipgrepConfig

@dataclass
class LinuxSandboxParams:
    command: str
    needs_network_restriction: bool
    http_socket_path: str | None = None
    socks_socket_path: str | None = None
    http_proxy_port: int | None = None
    socks_proxy_port: int | None = None
    read_config: FsReadRestrictionConfig | None = None
    write_config: FsWriteRestrictionConfig | None = None
    enable_weaker_nested_sandbox: bool | None = None
    allow_all_unix_sockets: bool | None = None
    bin_shell: str | None = None
    ripgrep_config: RipgrepConfig | None = None
    mandatory_deny_search_depth: int = 3
    allow_git_config: bool = False

async def wrap_command_linux(params: LinuxSandboxParams) -> str: ...
