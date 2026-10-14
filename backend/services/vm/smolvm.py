"""The smolvm backend: one QEMU microVM booted from a clean image the app builds itself.

**The clean image.** smolvm's stock Ubuntu guest, with ``backend/vm/provision.sh`` run on
it once with internet access. The resulting disk is flattened and kept under the data
dir, named by a hash of the script, so changing the script builds a new one. Every boot
starts an isolated copy of it, so nothing a guest did survives a restart.

**No network of its own.** The VM boots with smolvm's ``off`` policy (QEMU user
networking with ``restrict=on``): it cannot open a connection to the host, the LAN or the
internet. Host-to-guest port forwards still work, and they are the only way in or out —
see :mod:`services.vm.tunnel` for how egress rides on them.

QEMU on every host, with q35 rather than the microvm machine: the forwards need QEMU's
user networking, and q35 also boots under plain emulation where there is no KVM.

The SDK is synchronous, so every call runs in a worker thread.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import secrets
import shlex
import shutil
import subprocess
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any

from smolvm import GuestOS, InternetSettings, SmolVM, SmolVMError, VMConfig
from smolvm.utils import ensure_ssh_key

from .driver import ExecResult, VMError

logger = logging.getLogger(__name__)

PROVISION_SCRIPT = Path(__file__).resolve().parents[2] / "vm" / "provision.sh"
_GUEST_OS = "ubuntu"
_MACHINE = "q35"
_PROVISION_LOG = "/root/provision.log"
_PROVISION_EXIT = "/root/provision.exit"
_PROVISION_POLL_S = 5.0
_STEP_CHARS = 120
#: Every guest command starts from this environment. smolvm's vsock agent passes on its
#: init's, which exports no PATH, while its SSH channel passes a login's; neither is ours.
_GUEST_ENV = (
    "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "HOME=/root",
    "LANG=C.UTF-8",
)


@dataclass(frozen=True)
class CleanImage:
    disk: Path
    kernel: Path
    boot_args: str


def _guest_command(argv: list[str]) -> str:
    return shlex.join(["/usr/bin/env", "-i", *_GUEST_ENV, *argv])


def image_fingerprint(ca_bundle: Path | None = None) -> str:
    digest = hashlib.sha256(PROVISION_SCRIPT.read_bytes())
    digest.update(version("smolvm").encode())
    if ca_bundle is not None:
        digest.update(ca_bundle.read_bytes())
    return digest.hexdigest()[:16]


class SmolvmDriver:
    def __init__(
        self,
        *,
        state_dir: Path,
        cpus: int,
        memory_mb: int,
        disk_gb: int,
        boot_timeout_s: float,
        provision_timeout_s: float,
        image: Path | None = None,
        ca_bundle: Path | None = None,
    ) -> None:
        self._state = state_dir
        self._cpus = cpus
        self._memory_mb = memory_mb
        self._disk_mb = disk_gb * 1024
        self._boot_timeout_s = boot_timeout_s
        self._provision_timeout_s = provision_timeout_s
        self._image_override = image
        self._ca_bundle = ca_bundle
        self._vm: SmolVM | None = None
        #: What a boot in progress is doing: "building" the image, or "booting".
        self.phase: str | None = None
        #: The provisioning step a build is on.
        self.step: str | None = None

    @property
    def _smolvm_dir(self) -> Path:
        return self._state / "smolvm"

    @property
    def _current(self) -> Path:
        return self._state / "current-vm"

    async def boot(self) -> None:
        await asyncio.to_thread(self._boot)

    async def stop(self) -> None:
        await asyncio.to_thread(self._stop)

    async def alive(self) -> bool:
        if self._vm is None:
            return False
        try:
            result = await self.exec(["true"], timeout_s=15)
        except VMError:
            return False
        return result.exit_code == 0

    async def exec(self, argv: list[str], *, timeout_s: float) -> ExecResult:
        vm = self._require()
        try:
            result = await asyncio.to_thread(
                vm.run, _guest_command(argv), timeout=math.ceil(timeout_s), shell="raw"
            )
        except SmolVMError as exc:
            raise VMError(f"the platform VM did not run the command: {exc}") from exc
        return ExecResult(result.exit_code, result.stdout, result.stderr)

    async def put_file(self, local: Path, guest: str) -> None:
        vm = self._require()
        try:
            await asyncio.to_thread(vm.upload_file, local, guest)
        except (SmolVMError, OSError) as exc:
            raise VMError(f"could not copy a file into the platform VM: {exc}") from exc

    async def get_file(self, guest: str, local: Path, *, max_bytes: int) -> None:
        vm = self._require()
        try:
            await asyncio.to_thread(vm.download_file, guest, local, max_bytes=max_bytes)
        except (SmolVMError, OSError) as exc:
            raise VMError(f"could not copy a file out of the platform VM: {exc}") from exc

    async def forward(self, guest_port: int) -> int:
        vm = self._require()
        try:
            return await asyncio.to_thread(vm.expose_local, guest_port)
        except SmolVMError as exc:
            raise VMError(f"could not forward guest port {guest_port}: {exc}") from exc

    def _require(self) -> SmolVM:
        if self._vm is None:
            raise VMError("the platform VM is not running")
        return self._vm

    def _on_step(self, step: str) -> None:
        logger.info("platform VM: building the image: %s", step)
        self.step = step

    def _boot(self) -> None:
        try:
            self._boot_fresh()
        finally:
            self.phase = self.step = None

    def _boot_fresh(self) -> None:
        self._stop()
        self._discard_leftover()
        image = self._clean_image()
        self.phase, self.step = "booting", None
        private, _public = ensure_ssh_key(self._state / "keys")
        vm_id = f"hephaestus-{secrets.token_hex(4)}"
        config = self._config(vm_id, image, internet=False)
        self._current.parent.mkdir(parents=True, exist_ok=True)
        self._current.write_text(vm_id)
        try:
            vm = SmolVM(config=config, data_dir=self._smolvm_dir, ssh_key_path=str(private))
            vm.start(boot_timeout=self._boot_timeout_s)
            vm.wait_for_ready(timeout=self._boot_timeout_s)
        except SmolVMError as exc:
            raise VMError(f"the platform VM did not boot: {exc}") from exc
        self._vm = vm

    def _config(self, vm_id: str, image: CleanImage, *, internet: bool) -> VMConfig:
        """A VM booted from ``image``: the platform VM, with no network of its own, or a
        build resumed from a partial disk, with internet and the full disk size."""
        _private, public = ensure_ssh_key(self._state / "keys")
        return VMConfig(
            vm_id=vm_id,
            vcpu_count=self._cpus,
            memory=self._memory_mb if not internet else max(self._memory_mb, 2048),
            guest_os=GuestOS(_GUEST_OS),
            boot_mode="direct_kernel",
            kernel_path=image.kernel,
            rootfs_path=image.disk,
            rootfs_format="qcow2",
            boot_args=image.boot_args,
            ssh_capable=True,
            backend="qemu",
            qemu_machine=_MACHINE,
            disk_mode="isolated",
            disk_size_mib=self._disk_mb if internet else None,
            internet_settings=None if internet else InternetSettings(mode="off"),
            ssh_public_key=public.read_text().strip(),
        )

    def _stop(self) -> None:
        vm, self._vm = self._vm, None
        if vm is None:
            return
        with suppress(SmolVMError, OSError):
            vm.stop()
        with suppress(SmolVMError, OSError):
            vm.delete()
        self._current.unlink(missing_ok=True)

    def _discard_leftover(self) -> None:
        """Delete the VM an earlier process left running, if it crashed before stopping
        it."""
        try:
            vm_id = self._current.read_text().strip()
        except OSError:
            return
        with suppress(Exception):
            SmolVM.from_id(vm_id, data_dir=self._smolvm_dir).delete()
        self._current.unlink(missing_ok=True)

    def _clean_image(self) -> CleanImage:
        if self._image_override is not None:
            return _read_image(self._image_override)
        target = self._state / "images" / image_fingerprint(self._ca_bundle)
        if (target / "image.json").is_file():
            return _read_image(target)
        self._build_image(target)
        return _read_image(target)

    def _build_image(self, target: Path) -> None:
        """Provision a clean image into ``target``. A build that fails keeps its disk as
        the partial image the next attempt resumes from; the provision script skips the
        stages that disk already finished."""
        logger.info("platform VM: building the clean image (once per provision script)")
        self.phase = "building"
        building = target.with_name(f".{target.name}.building")
        shutil.rmtree(building, ignore_errors=True)
        building.mkdir(parents=True)
        partial = self._partial_image()
        private, _public = ensure_ssh_key(self._state / "keys")
        try:
            if partial is not None:
                logger.info("platform VM: resuming the image build from its partial disk")
                vm = SmolVM(
                    config=self._config(
                        f"hephaestus-build-{secrets.token_hex(4)}", partial, internet=True
                    ),
                    data_dir=self._smolvm_dir,
                    ssh_key_path=str(private),
                )
            else:
                vm = SmolVM(
                    backend="qemu",
                    os=_GUEST_OS,
                    qemu_machine=_MACHINE,
                    memory=max(self._memory_mb, 2048),
                    disk_size=self._disk_mb,
                    data_dir=self._smolvm_dir,
                    ssh_key_path=str(private),
                )
        except SmolVMError as exc:
            raise VMError(f"could not create the provisioning VM: {exc}") from exc
        try:
            vm.start(boot_timeout=self._boot_timeout_s)
            vm.wait_for_ready(timeout=self._boot_timeout_s)
            vm.upload_file(PROVISION_SCRIPT, "/root/provision.sh")
            if self._ca_bundle is not None:
                vm.upload_file(self._ca_bundle, "/root/extra-ca.crt")
            argv = ["bash", "/root/provision.sh"]
            if self._ca_bundle is not None:
                argv.append("/root/extra-ca.crt")
            try:
                provision(vm, argv, timeout_s=self._provision_timeout_s, on_step=self._on_step)
            except VMError:
                self._keep_partial(vm)
                raise
            config = vm.info.config
            vm.stop()
            _save_image(config, building)
        except SmolVMError as exc:
            raise VMError(f"building the platform VM image failed: {exc}") from exc
        finally:
            with suppress(SmolVMError, OSError):
                vm.delete()
        shutil.rmtree(target, ignore_errors=True)
        building.rename(target)
        shutil.rmtree(self._partial_dir, ignore_errors=True)

    @property
    def _partial_dir(self) -> Path:
        return self._state / "images" / ".partial"

    def _partial_image(self) -> CleanImage | None:
        """The disk a failed build left, if it was made by this smolvm release."""
        try:
            meta = json.loads((self._partial_dir / "image.json").read_text())
        except OSError, ValueError:
            return None
        if meta.get("smolvm") != version("smolvm"):
            shutil.rmtree(self._partial_dir, ignore_errors=True)
            return None
        return _read_image(self._partial_dir)

    def _keep_partial(self, vm: SmolVM) -> None:
        """Save a failed build's disk for the next attempt. Best-effort: without it the
        next attempt starts from scratch."""
        saving = self._partial_dir.with_name(".partial.saving")
        try:
            config = vm.info.config
            vm.stop()
            shutil.rmtree(saving, ignore_errors=True)
            saving.mkdir(parents=True)
            _save_image(config, saving)
            shutil.rmtree(self._partial_dir, ignore_errors=True)
            saving.rename(self._partial_dir)
        except SmolVMError, OSError, subprocess.CalledProcessError:
            logger.warning("platform VM: could not keep the partial image", exc_info=True)
            shutil.rmtree(saving, ignore_errors=True)


def provision(
    vm: Any, argv: list[str], *, timeout_s: float, on_step: Callable[[str], None]
) -> None:
    """Run the provision script detached in the guest and follow its log, so a build that
    takes many minutes reports each step it reaches."""
    script = shlex.join(argv)
    vm.run(
        _guest_command(
            [
                "sh",
                "-c",
                f"rm -f {_PROVISION_LOG} {_PROVISION_EXIT}; "
                f"setsid sh -c '{script} > {_PROVISION_LOG} 2>&1; "
                f"echo $? > {_PROVISION_EXIT}' > /dev/null 2>&1 < /dev/null &",
            ]
        ),
        timeout=60,
        shell="raw",
    )
    deadline = time.monotonic() + timeout_s
    last_step = ""
    while time.monotonic() < deadline:
        time.sleep(_PROVISION_POLL_S)
        probe = vm.run(
            _guest_command(
                [
                    "sh",
                    "-c",
                    f"cat {_PROVISION_EXIT} 2>/dev/null; echo; "
                    f"grep '^+ ' {_PROVISION_LOG} | tail -n 1",
                ]
            ),
            timeout=60,
            shell="raw",
        )
        status, _, last = probe.stdout.partition("\n")
        step = last.strip().removeprefix("+ ")[:_STEP_CHARS]
        if step and step != last_step:
            last_step = step
            on_step(step)
        if status.strip():
            if status.strip() != "0":
                tail = vm.run(
                    _guest_command(["tail", "-c", "2000", _PROVISION_LOG]),
                    timeout=60,
                    shell="raw",
                )
                raise VMError(f"provisioning the platform VM failed: {tail.stdout.strip()}")
            return
    raise VMError("provisioning the platform VM timed out")


def _save_image(config: VMConfig, directory: Path) -> None:
    assert config.rootfs_path is not None
    _flatten(Path(config.rootfs_path), directory / "disk.qcow2")
    shutil.copyfile(Path(str(config.kernel_path)), directory / "vmlinux")
    (directory / "image.json").write_text(
        json.dumps({"boot_args": config.boot_args, "smolvm": version("smolvm")})
    )


def _read_image(directory: Path) -> CleanImage:
    meta = json.loads((directory / "image.json").read_text())
    return CleanImage(
        disk=directory / "disk.qcow2",
        kernel=directory / "vmlinux",
        boot_args=meta["boot_args"],
    )


def _flatten(source: Path, target: Path) -> None:
    """Write ``source`` and its backing chain out as one standalone qcow2."""
    try:
        subprocess.run(
            ["qemu-img", "convert", "-O", "qcow2", str(source), str(target)],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise VMError(f"could not write the platform VM image: {exc}") from exc
