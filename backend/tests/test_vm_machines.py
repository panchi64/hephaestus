"""One machine per conversation: when it is created, reused, restarted, reaped and purged,
and the spec, allowlist and address range a machine is given."""

from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest

from core.container import ServiceContainer
from runs import Run, RunStream
from services.egress import EgressPolicy
from services.home.backend import Mount
from services.home.paths import SCRATCH, Place
from services.home.table import RunHome
from services.home.workspace import machine_spec
from services.offline import OfflineModeService
from services.vm import ConversationMachines, GuestImage, MachineUnavailableError, Platform
from services.vm.machines import Bind, MachineSpec
from services.vm.services import public_cidrs
from tools import RunDeps
from tools.home_workspace import allowed_hosts

from .vm_support import FakeSmol, install_fake_smol, ready_platform

SPEC_A = MachineSpec(binds=(Bind("/host/a", "/home/agent/scratch", False),), allow_hosts=None)
SPEC_B = MachineSpec(
    binds=(Bind("/host/a", "/home/agent/scratch", False),), allow_hosts=("pypi.org",)
)


async def _pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    idle_ttl_s: float = 3600.0,
    max_machines: int = 4,
) -> tuple[ConversationMachines, FakeSmol]:
    fake = install_fake_smol(monkeypatch)
    pool = ConversationMachines(
        await ready_platform(tmp_path),
        prefix="heph-test",
        cpus=1,
        memory_mb=256,
        idle_ttl_s=idle_ttl_s,
        reap_interval_s=3600.0,
        max_machines=max_machines,
    )
    return pool, fake


async def _touch_use(pool: ConversationMachines, key: str, spec: MachineSpec = SPEC_A) -> None:
    async with pool.use(key, spec, working_dir="/home/agent/scratch"):
        pass


class TestCreateAndReuse:
    async def test_a_machine_is_created_once_and_reused_for_the_same_spec(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        pool, fake = await _pool(tmp_path, monkeypatch)

        await _touch_use(pool, "conv-1")
        await _touch_use(pool, "conv-1")

        assert len(fake.machines) == 1
        assert pool.running("conv-1")
        assert pool.machine("conv-1") is fake.machines[0]

    async def test_a_machine_boots_with_the_specs_mounts_and_allowlist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        pool, fake = await _pool(tmp_path, monkeypatch)

        await _touch_use(pool, "Conv_1", SPEC_B)

        config = fake.machines[0].config
        assert config.name == "heph-test-conv-1"
        assert config.mounts is not None
        [mount] = config.mounts
        assert (mount.source, mount.target, mount.read_only) == (
            "/host/a",
            "/home/agent/scratch",
            False,
        )
        assert config.resources is not None
        assert config.resources.allow_hosts == ["pypi.org"]
        assert "heph-test-conv-1" in fake.deleted_names

    async def test_no_allowlist_means_no_network_rule_is_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        pool, fake = await _pool(tmp_path, monkeypatch)

        await _touch_use(pool, "conv-1", SPEC_A)

        config = fake.machines[0].config
        assert config.resources is not None
        assert not config.resources.allow_hosts

    async def test_each_conversation_has_its_own_machine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        pool, fake = await _pool(tmp_path, monkeypatch)

        await _touch_use(pool, "a")
        await _touch_use(pool, "b")

        assert len(fake.machines) == 2
        assert pool.machine("a") is not pool.machine("b")

    async def test_a_backend_is_kept_per_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        pool, _ = await _pool(tmp_path, monkeypatch)

        async with pool.use("c", SPEC_A, working_dir="/home/agent/scratch") as first:
            pass
        async with pool.use("c", SPEC_A, working_dir="/home/agent/scratch") as again:
            pass
        async with pool.use("c", SPEC_A, working_dir="/home/agent/projects/x") as other:
            pass

        assert again is first
        assert other is not first

    async def test_a_machine_that_will_not_start_is_reported_and_not_kept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import smol

        pool, fake = await _pool(tmp_path, monkeypatch)
        fake.create_error = smol.SmolError("INTERNAL", "no memory")

        with pytest.raises(MachineUnavailableError, match="did not start"):
            await _touch_use(pool, "conv-1")

        assert not pool.running("conv-1")

    async def test_nothing_boots_while_the_platform_is_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        fake = install_fake_smol(monkeypatch)
        platform = Platform(GuestImage(tmp_path / "images"), enabled=False)
        await platform.start()
        pool = ConversationMachines(
            platform,
            prefix="heph-test",
            cpus=1,
            memory_mb=256,
            idle_ttl_s=60.0,
            reap_interval_s=60.0,
            max_machines=2,
        )

        with pytest.raises(MachineUnavailableError, match="disabled"):
            await _touch_use(pool, "conv-1")

        assert fake.machines == []


class TestRestart:
    async def test_a_changed_spec_replaces_the_machine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        pool, fake = await _pool(tmp_path, monkeypatch)
        await _touch_use(pool, "conv-1", SPEC_A)

        await _touch_use(pool, "conv-1", SPEC_B)

        assert len(fake.machines) == 2
        assert fake.machines[0].deleted and not fake.machines[1].deleted
        assert pool.machine("conv-1") is fake.machines[1]

    async def test_a_restart_waits_for_the_command_in_flight(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        pool, fake = await _pool(tmp_path, monkeypatch)
        entered = asyncio.Event()
        leave = asyncio.Event()

        async def hold() -> None:
            async with pool.use("conv-1", SPEC_A, working_dir="/home/agent/scratch"):
                entered.set()
                _ = await leave.wait()

        holder = asyncio.create_task(hold())
        _ = await entered.wait()
        restart = asyncio.create_task(_touch_use(pool, "conv-1", SPEC_B))
        for _ in range(5):
            await asyncio.sleep(0)

        assert not restart.done()
        assert not fake.machines[0].deleted

        leave.set()
        _ = await asyncio.gather(holder, restart)

        assert fake.machines[0].deleted
        assert len(fake.machines) == 2


class TestReaping:
    async def test_a_sweep_reaps_idle_machines_and_leaves_busy_ones(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        pool, fake = await _pool(tmp_path, monkeypatch, idle_ttl_s=0.0)
        await _touch_use(pool, "idle")

        async with pool.use("busy", SPEC_A, working_dir="/home/agent/scratch"):
            await pool.sweep()
            assert not pool.running("idle")
            assert pool.running("busy")

        assert fake.machines[0].deleted
        assert not fake.machines[1].deleted

    async def test_a_sweep_keeps_a_machine_used_within_the_ttl(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        pool, _ = await _pool(tmp_path, monkeypatch, idle_ttl_s=3600.0)
        await _touch_use(pool, "conv-1")

        await pool.sweep()

        assert pool.running("conv-1")

    async def test_release_deletes_the_machine_and_a_later_use_makes_another(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        pool, fake = await _pool(tmp_path, monkeypatch)
        await _touch_use(pool, "conv-1")

        await pool.release("conv-1")
        await pool.release("never-had-one")
        await _touch_use(pool, "conv-1")

        assert fake.machines[0].deleted
        assert len(fake.machines) == 2

    async def test_stop_deletes_every_machine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        pool, fake = await _pool(tmp_path, monkeypatch)
        await _touch_use(pool, "a")
        await _touch_use(pool, "b")

        await pool.stop()

        assert all(m.deleted for m in fake.machines)
        assert not pool.running("a") and not pool.running("b")


class TestTheCap:
    async def test_the_least_recently_used_idle_machine_makes_room(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        pool, fake = await _pool(tmp_path, monkeypatch, max_machines=2)
        await _touch_use(pool, "old")
        await _touch_use(pool, "newer")
        pool.touch("old")

        await _touch_use(pool, "third")

        assert not pool.running("newer")
        assert pool.running("old") and pool.running("third")
        assert fake.machines[1].deleted

    async def test_when_every_machine_is_busy_a_new_conversation_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        pool, _ = await _pool(tmp_path, monkeypatch, max_machines=1)

        async with pool.use("busy", SPEC_A, working_dir="/home/agent/scratch"):
            with pytest.raises(MachineUnavailableError, match="busy"):
                await _touch_use(pool, "other")

        assert pool.running("busy") and not pool.running("other")

    async def test_a_busy_machine_is_never_the_one_evicted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        pool, _ = await _pool(tmp_path, monkeypatch, max_machines=2)
        await _touch_use(pool, "idle")

        async with pool.use("busy", SPEC_A, working_dir="/home/agent/scratch"):
            await _touch_use(pool, "third")
            assert pool.running("busy") and pool.running("third")
            assert not pool.running("idle")


class TestPurge:
    async def test_it_removes_the_conversation_and_its_delegates_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        pool, _ = await _pool(tmp_path, monkeypatch)
        for key in ("c1", "c1/agent", "c1/agent/deeper", "c10", "c2"):
            await _touch_use(pool, key)

        await pool.purge("c1")

        assert not pool.running("c1")
        assert not pool.running("c1/agent")
        assert not pool.running("c1/agent/deeper")
        assert pool.running("c10") and pool.running("c2")


class TestEndHooks:
    async def test_a_hook_hears_of_every_way_a_machine_ends(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        pool, _ = await _pool(tmp_path, monkeypatch, idle_ttl_s=0.0, max_machines=2)
        ended: list[str] = []
        pool.on_end(ended.append)

        await _touch_use(pool, "released")
        await pool.release("released")
        await _touch_use(pool, "swept")
        await pool.sweep()
        await _touch_use(pool, "restarted", SPEC_A)
        await _touch_use(pool, "restarted", SPEC_B)
        await pool.purge("restarted")
        await _touch_use(pool, "a")
        await _touch_use(pool, "b")
        await _touch_use(pool, "evicting")

        assert ended == ["released", "swept", "restarted", "restarted", "a"]

    async def test_a_machine_that_is_kept_fires_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        pool, _ = await _pool(tmp_path, monkeypatch)
        ended: list[str] = []
        pool.on_end(ended.append)

        await _touch_use(pool, "conv-1")
        await _touch_use(pool, "conv-1")

        assert ended == []


def _home(root: Path, mounts: Iterable[tuple[Place, bool]]) -> RunHome:
    built = tuple(
        Mount(place, root / place.path.strip("/"), writable) for place, writable in mounts
    )
    return RunHome(mounts=built, default=SCRATCH, key="t")


class TestMachineSpec:
    def test_every_place_is_bound_at_its_home_path(self, tmp_path: Path):
        home = _home(tmp_path, [(SCRATCH, True), (Place("worktrees", "app"), True)])

        spec = machine_spec(home, None)

        targets = [bind.target for bind in spec.binds]
        assert "/home/agent/scratch" in targets
        assert "/home/agent/worktrees/app" in targets
        assert spec.allow_hosts is None

    def test_scratch_is_also_bound_at_the_work_alias(self, tmp_path: Path):
        home = _home(tmp_path, [(SCRATCH, True)])

        spec = machine_spec(home, None)

        scratch = next(b for b in spec.binds if b.target == "/home/agent/scratch")
        alias = next(b for b in spec.binds if b.target == "/work")
        assert alias.source == scratch.source
        assert not alias.read_only

    def test_a_place_the_thread_may_not_write_is_bound_read_only(self, tmp_path: Path):
        home = _home(tmp_path, [(SCRATCH, True), (Place("projects", "app"), False)])

        spec = machine_spec(home, None)

        project = next(b for b in spec.binds if b.target == "/home/agent/projects/app")
        scratch = next(b for b in spec.binds if b.target == "/home/agent/scratch")
        assert project.read_only is True
        assert scratch.read_only is False

    def test_a_home_without_scratch_has_no_work_alias(self, tmp_path: Path):
        home = _home(tmp_path, [(Place("projects", "app"), False)])

        spec = machine_spec(home, None)

        assert "/work" not in [b.target for b in spec.binds]

    def test_the_allowlist_is_carried_into_the_spec(self, tmp_path: Path):
        spec = machine_spec(_home(tmp_path, [(SCRATCH, True)]), ("pypi.org",))

        assert spec.allow_hosts == ("pypi.org",)

    def test_a_lease_that_widens_a_place_changes_the_spec(self, tmp_path: Path):
        project = Place("projects", "app")
        home = _home(tmp_path, [(SCRATCH, True), (project, False)])

        assert machine_spec(home, None) != machine_spec(home.with_writable(project), None)


@dataclass
class _OfflineState:
    effective_offline: bool


class _Offline:
    def __init__(self, *, offline: bool) -> None:
        self._offline: bool = offline

    def state(self) -> _OfflineState:
        return _OfflineState(self._offline)


class _Policy:
    def __init__(self, domains: Iterable[str]) -> None:
        self._domains: frozenset[str] = frozenset(domains)

    async def allowed_for(self, _key: str) -> frozenset[str]:
        return self._domains


def _deps(*, policy: _Policy | None, offline: _Offline | None = None) -> RunDeps:
    caps = ServiceContainer()
    if policy is not None:
        caps.add(policy, as_type=EgressPolicy)
    if offline is not None:
        caps.add(offline, as_type=OfflineModeService)
    run = Run(id="r", kind="chat", owner_id="operator", stream=RunStream())
    return RunDeps(run=run, owner_id="operator", caps=caps, conversation_id="conv-1")


class TestAllowedHosts:
    async def test_offline_mode_leaves_no_network(self):
        deps = _deps(policy=_Policy(["pypi.org"]), offline=_Offline(offline=True))

        assert await allowed_hosts(deps) is None

    async def test_an_empty_allowlist_leaves_no_network(self):
        assert await allowed_hosts(_deps(policy=_Policy([]))) is None

    async def test_no_policy_leaves_no_network(self):
        assert await allowed_hosts(_deps(policy=None)) is None

    async def test_a_wildcard_is_the_name_smol_matches_with_its_subdomains(self):
        deps = _deps(
            policy=_Policy(["*.github.com", "pypi.org", "github.com"]),
            offline=_Offline(offline=False),
        )

        assert await allowed_hosts(deps) == ("github.com", "pypi.org")


class TestPublicCidrs:
    @staticmethod
    def _covers(address: str) -> bool:
        ip = ipaddress.ip_address(address)
        return any(ip in ipaddress.ip_network(cidr) for cidr in public_cidrs())

    def test_a_public_address_is_covered(self):
        assert self._covers("8.8.8.8")
        assert self._covers("1.1.1.1")
        assert self._covers("172.32.0.1")
        assert self._covers("11.0.0.1")

    @pytest.mark.parametrize(
        "address",
        [
            "10.1.2.3",
            "192.168.1.1",
            "127.0.0.1",
            "169.254.169.254",
            "172.16.0.1",
            "172.31.255.255",
            "100.64.0.1",
            "0.1.2.3",
            "224.0.0.1",
            "255.255.255.255",
        ],
    )
    def test_a_private_or_reserved_address_is_not(self, address: str):
        assert not self._covers(address)

    def test_the_ranges_are_the_internet_minus_the_blocked_blocks_with_no_overlap(self):
        networks = [ipaddress.IPv4Network(cidr) for cidr in public_cidrs()]
        blocked = [
            "0.0.0.0/8",
            "10.0.0.0/8",
            "100.64.0.0/10",
            "127.0.0.0/8",
            "169.254.0.0/16",
            "172.16.0.0/12",
            "192.0.0.0/24",
            "192.168.0.0/16",
            "198.18.0.0/15",
            "224.0.0.0/3",
        ]
        total = sum(n.num_addresses for n in networks)
        assert total == 2**32 - sum(ipaddress.IPv4Network(b).num_addresses for b in blocked)
        assert sum(n.num_addresses for n in ipaddress.collapse_addresses(networks)) == total
