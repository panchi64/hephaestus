"""The guest image every machine boots: built once by ``backend/vm/build_image.sh``.

The script runs in an ``ubuntu:24.04`` builder machine with internet access, provisions
the builder itself and packs its root filesystem as a ``docker save`` archive. The archive
is kept under the data dir, named by a hash of the script and the extra CA, so changing
either builds a new one.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
from pathlib import Path

import smol

from .errors import ImageBuildError
from .target import LOCAL

logger = logging.getLogger(__name__)

BUILD_SCRIPT = Path(__file__).resolve().parents[2] / "vm" / "build_image.sh"
#: Files the script installs into the image, beside it in ``/out``.
_INSTALLED = (Path(__file__).resolve().parents[2] / "vm" / "portfwd.py",)
_BUILDER_IMAGE = "ubuntu:24.04"
_BUILD_TIMEOUT_S = 3600
_POLL_S = 5.0
_STEP_CHARS = 120


def fingerprint(ca_bundle: Path | None) -> str:
    digest = hashlib.sha256(BUILD_SCRIPT.read_bytes())
    for installed in _INSTALLED:
        digest.update(installed.read_bytes())
    if ca_bundle is not None:
        digest.update(ca_bundle.read_bytes())
    return digest.hexdigest()[:16]


class GuestImage:
    """The archive machines boot, built on first need. ``override`` is a prebuilt one."""

    def __init__(
        self,
        images_dir: Path,
        *,
        ca_bundle: Path | None = None,
        override: Path | None = None,
        cpus: int = 4,
        memory_mb: int = 4096,
    ) -> None:
        self._dir: Path = images_dir
        self._ca: Path | None = ca_bundle
        self._override: Path | None = override
        self._cpus: int = cpus
        self._memory_mb: int = memory_mb
        self._lock: asyncio.Lock = asyncio.Lock()
        #: The build step under way, for the operator; None when no build runs.
        self.step: str | None = None
        self.building: bool = False

    @property
    def path(self) -> Path:
        if self._override is not None:
            return self._override
        return self._dir / fingerprint(self._ca) / "image.tar"

    async def ensure(self) -> Path:
        """The archive's path, building it first when it is missing."""
        async with self._lock:
            target = self.path
            if target.is_file():
                return target
            if self._override is not None:
                raise ImageBuildError(f"the configured VM image {target} does not exist")
            self.building = True
            try:
                await self._build(target)
            finally:
                self.building = False
                self.step = None
            return target

    async def _build(self, target: Path) -> None:
        work = target.parent.with_name(target.parent.name + ".build")
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        for source in (BUILD_SCRIPT, *_INSTALLED):
            _ = shutil.copyfile(source, work / source.name)
        argv = ["bash", "/out/build_image.sh"]
        if self._ca is not None:
            _ = shutil.copyfile(self._ca, work / "extra-ca.crt")
            argv.append("/out/extra-ca.crt")
        logger.info("guest image: building %s", target.parent.name)
        builder = await smol.AsyncMachine.create(
            smol.MachineConfig(
                name=f"hephaestus-build-{target.parent.name}-{os.getpid()}",
                image=_BUILDER_IMAGE,
                mounts=[smol.MountSpec(source=str(work), target="/out")],
                resources=smol.ResourceSpec(
                    network=True, cpus=self._cpus, memory_mb=self._memory_mb, storage_gb=16
                ),
                persistent=True,
            ),
            LOCAL,
        )
        watcher = asyncio.create_task(self._watch(work / "build.log"))
        try:
            script = f"{' '.join(argv)} > /out/build.log 2>&1"
            result = await builder.exec(
                ["bash", "-c", script], smol.ExecOptions(timeout=_BUILD_TIMEOUT_S)
            )
        finally:
            _ = watcher.cancel()
            _ = await builder.delete()
        built = work / "image.tar"
        if result.exit_code != 0 or not built.is_file():
            raise ImageBuildError(
                f"building the VM image failed at: {_last_step(work / 'build.log') or 'start'}"
                + f" (log: {work / 'build.log'})"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        _ = built.replace(target)
        shutil.rmtree(work, ignore_errors=True)
        logger.info("guest image: built %s", target)

    async def _watch(self, log: Path) -> None:
        while True:
            step = _last_step(log)
            if step and step != self.step:
                self.step = step
                logger.info("guest image: %s", step)
            await asyncio.sleep(_POLL_S)


def _last_step(log: Path) -> str | None:
    """The last command the script traced, from the tail of its ``set -x`` log."""
    try:
        with log.open("rb") as handle:
            _ = handle.seek(max(0, log.stat().st_size - 8192))
            tail = handle.read().decode(errors="replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        if line.startswith("+ "):
            return line.removeprefix("+ ")[:_STEP_CHARS]
    return None
