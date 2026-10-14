from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .config import SandboxRuntimeConfig
from .utils.platform import Platform
from .utils.ripgrep import RipgrepConfig

@dataclass
class NetworkHostPattern:
    host: str
    port: int | None = None

_config: SandboxRuntimeConfig | None

class SandboxManager:
    @staticmethod
    async def initialize(
        runtime_config: SandboxRuntimeConfig,
        sandbox_ask_callback: Callable[[NetworkHostPattern], Awaitable[bool]] | None = None,
        enable_log_monitor: bool = False,
    ) -> None: ...
    @staticmethod
    def is_supported_platform(platform: Platform) -> bool: ...
    @staticmethod
    def check_dependencies(ripgrep_config: RipgrepConfig | None = None) -> bool: ...
    @staticmethod
    async def wrap_with_sandbox(
        command: str,
        bin_shell: str | None = None,
        custom_config: SandboxRuntimeConfig | None = None,
    ) -> str: ...
    @staticmethod
    def get_config() -> SandboxRuntimeConfig | None: ...
    @staticmethod
    def update_config(new_config: SandboxRuntimeConfig) -> None: ...
    @staticmethod
    async def reset() -> None: ...
