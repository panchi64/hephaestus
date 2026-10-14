from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

RootfsFormat = Literal["raw-ext4", "qcow2"]
QemuMachine = Literal["auto", "q35", "microvm"]

class GuestOS(StrEnum):
    ALPINE = "alpine"
    UBUNTU = "ubuntu"
    WINDOWS = "windows"
    MACOS = "macos"

class InternetSettings(BaseModel):
    mode: Literal["open", "off", "restricted"] | None = None
    allowed_cidrs: tuple[str, ...] = ()
    allowed_domains: tuple[str, ...] = ("*",)
    allowed_http_methods: tuple[str, ...] = ("*",)

class VMConfig(BaseModel):
    vm_id: str = Field(default_factory=str)
    vcpu_count: int = 2
    memory: int = 512
    guest_os: GuestOS = GuestOS.ALPINE
    boot_mode: Literal["direct_kernel", "firmware", "platform"] = "direct_kernel"
    kernel_path: Path | None = None
    rootfs_path: Path | None = None
    rootfs_format: RootfsFormat | None = None
    boot_args: str = "console=ttyS0 reboot=k panic=1 pci=off"
    ssh_capable: bool = False
    backend: str | None = None
    qemu_machine: QemuMachine = "auto"
    disk_mode: Literal["isolated", "shared"] = "isolated"
    disk_size_mib: int | None = None
    internet_settings: InternetSettings | None = None
    ssh_public_key: str | None = None

class CommandResult(BaseModel):
    exit_code: int
    stdout: str
    stderr: str

class VMInfo(BaseModel):
    vm_id: str
    config: VMConfig
