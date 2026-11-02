"""A preview's dev server is the agent's: started, checked and stopped as its account."""

from __future__ import annotations

from pathlib import Path

import pytest

from core import net
from services.vm import ConversationMachines
from services.vm.machines import GUEST_USER, Bind, MachineSpec
from services.vm.previews import Previews

from .vm_support import install_fake_smol, ready_platform

SPEC = MachineSpec(binds=(Bind("/host/a", "/home/agent/scratch", False),), allow_hosts=None)


async def test_every_preview_command_runs_as_the_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fake = install_fake_smol(monkeypatch)

    async def ready(_url: str, _timeout_s: float) -> bool:
        return True

    monkeypatch.setattr(net, "await_http_ready", ready)
    machines = ConversationMachines(
        await ready_platform(tmp_path),
        prefix="heph-test",
        cpus=1,
        memory_mb=256,
        idle_ttl_s=60.0,
        reap_interval_s=60.0,
        max_machines=2,
    )
    previews = Previews(machines, startup_timeout_s=1.0)

    _ = await previews.start(
        "conv-1", SPEC, working_dir="/home/agent/scratch", command=["npm", "run", "dev"], port=5173
    )
    await previews.stop("conv-1")

    (machine,) = fake.machines
    preview_opts = machine.options[1:]
    assert preview_opts
    assert all(opts is not None and opts.user == GUEST_USER for opts in preview_opts)
