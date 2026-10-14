from collections.abc import Callable
from pathlib import Path
from typing import Literal

from .types import CommandResult, GuestOS, InternetSettings, QemuMachine, VMConfig, VMInfo

class SmolVM:
    def __init__(
        self,
        config: VMConfig | None = None,
        *,
        vm_id: str | None = None,
        image: str | None = None,
        data_dir: Path | None = None,
        socket_dir: Path | None = None,
        backend: str | None = None,
        os: GuestOS | str | None = None,
        qemu_machine: QemuMachine = "auto",
        memory: int | None = None,
        disk_size: int | None = None,
        ssh_user: str = "root",
        ssh_key_path: str | None = None,
        ssh_password: str | None = None,
        internet_settings: InternetSettings | dict[str, object] | None = None,
        mounts: list[str] | None = None,
        writable_mounts: bool = False,
        on_download: Callable[[str, int, int | None], None] | None = None,
    ) -> None: ...
    @classmethod
    def from_id(
        cls,
        vm_id: str,
        *,
        data_dir: Path | None = None,
        socket_dir: Path | None = None,
        backend: str | None = None,
        ssh_user: str = "root",
        ssh_key_path: str | None = None,
        ssh_password: str | None = None,
    ) -> SmolVM: ...
    def start(
        self, boot_timeout: float = 30.0, *, on_progress: Callable[[str], None] | None = None
    ) -> SmolVM: ...
    def stop(self, timeout: float = 3.0) -> SmolVM: ...
    def delete(self) -> None: ...
    def run(
        self, command: str, timeout: int = 30, shell: Literal["login", "raw"] = "login"
    ) -> CommandResult: ...
    def upload_file(
        self, local_path: str | Path, guest_path: str, *, make_dirs: bool = True
    ) -> str: ...
    def download_file(
        self,
        guest_path: str,
        local_path: str | Path,
        *,
        make_dirs: bool = True,
        max_bytes: int | None = None,
    ) -> str: ...
    def wait_for_ready(
        self, timeout: float = 60.0, *, on_progress: Callable[[str], None] | None = None
    ) -> SmolVM: ...
    def expose_local(
        self, guest_port: int, host_port: int | None = None, *, guest_loopback: bool = False
    ) -> int: ...
    @property
    def info(self) -> VMInfo: ...
