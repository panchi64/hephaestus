from pydantic import BaseModel

IgnoreViolationsConfig = dict[str, list[str]]

class NetworkConfig(BaseModel):
    allowed_domains: list[str] = []
    denied_domains: list[str] = []
    allow_unix_sockets: list[str] | None = None
    allow_all_unix_sockets: bool | None = None
    allow_local_binding: bool | None = None
    http_proxy_port: int | None = None
    socks_proxy_port: int | None = None

class FilesystemConfig(BaseModel):
    deny_read: list[str] = []
    allow_write: list[str] = []
    deny_write: list[str] = []
    allow_git_config: bool | None = None

class RipgrepConfig(BaseModel):
    command: str = "rg"
    args: list[str] | None = None

class ResourceLimitsConfig(BaseModel):
    max_memory_mb: int | None = None
    max_cpu_seconds: int | None = None
    max_file_size_mb: int | None = None
    max_processes: int | None = None

class SandboxRuntimeConfig(BaseModel):
    network: NetworkConfig
    filesystem: FilesystemConfig
    ignore_violations: IgnoreViolationsConfig | None = None
    enable_weaker_nested_sandbox: bool | None = None
    ripgrep: RipgrepConfig | None = None
    mandatory_deny_search_depth: int | None = None
    allow_pty: bool | None = None
    resource_limits: ResourceLimitsConfig | None = None
