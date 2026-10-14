"""What keeps two dev instances — and a dev instance and the operator's — apart.

Every test here roots the instance store under ``tmp_path``, because the real one is the
developer's own ``~/.hephaestus/dev`` and a test that allocated a slot in it would hand a
port to a worktree that had not asked for one.
"""

from __future__ import annotations

import json

import pytest

from devkit import ports
from devkit.instance import DevInstance, claimed_slots, resolve


@pytest.fixture(autouse=True)
def _ports_are_free(monkeypatch):
    """Take the host's own ports out of the picture.

    Without this the suite's answers depend on what happens to be listening — a real dev
    instance running on slot 0 would push every assertion here up by one, and the tests
    would fail for the developer who had actually used the thing they test.
    """
    monkeypatch.setattr(ports, "is_free", lambda port: True)


def test_the_first_instance_takes_slot_zero(tmp_path):
    instance = resolve(root=tmp_path, name="alpha")
    assert instance.slot == 0
    assert instance.backend_port == ports.BACKEND_BASE
    assert instance.root == tmp_path / "alpha"


def test_a_second_worktree_gets_its_own_slot(tmp_path):
    first = resolve(root=tmp_path, name="alpha")
    second = resolve(root=tmp_path, name="beta")
    assert second.slot != first.slot
    assert set(second.env()) == set(first.env())
    for key in ("HEPHAESTUS_PORT", "HEPHAESTUS_DATA_DIR"):
        assert second.env()[key] != first.env()[key]


def test_a_claimed_slot_is_never_handed_out_again_even_while_nothing_runs(tmp_path):
    # The failure this guards: an instance nobody has started today owns its ports just
    # as much as a running one, and allocating by "what is listening" would give them
    # away and break the first instance the next time anyone brought it up.
    resolve(root=tmp_path, name="alpha")
    assert claimed_slots(tmp_path) == {0}
    assert resolve(root=tmp_path, name="beta").slot == 1


def test_an_unreadable_neighbour_does_not_block_allocation(tmp_path):
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "instance.json").write_text("{ not json")
    assert claimed_slots(tmp_path) == set()
    assert resolve(root=tmp_path, name="alpha").slot == 0


def test_resolving_twice_returns_the_same_instance(tmp_path):
    first = resolve(root=tmp_path, name="alpha")
    again = resolve(root=tmp_path, name="alpha")
    assert (again.slot, again.password) == (first.slot, first.password)


def test_flags_are_per_run_but_the_workspace_is_not(tmp_path):
    first = resolve(root=tmp_path, name="alpha")
    with_auth = resolve(root=tmp_path, name="alpha", auth=True, vm=True)
    assert (with_auth.auth, with_auth.vm) == (True, True)
    assert with_auth.password == first.password  # the vault key never changes underneath
    assert resolve(root=tmp_path, name="alpha").auth is True  # and the choice persists


def test_a_flag_that_sticks_can_also_be_turned_back_off(tmp_path):
    # The bug: the CLI mapped an absent flag to False and then to None, so `--auth` and
    # `--with-vm` could only ever be switched on otherwise. An instance that once ran with
    # the VM would boot it on every later `up`, with no way to say otherwise.
    resolve(root=tmp_path, name="alpha", auth=True, vm=True)
    off = resolve(root=tmp_path, name="alpha", auth=False, vm=False)
    assert (off.auth, off.vm) == (False, False)
    assert resolve(root=tmp_path, name="alpha").vm is False


def test_resolving_without_a_change_does_not_rewrite_the_state_file(tmp_path):
    # `status`, `doctor` and `stop` all resolve in order to *read*. Rewriting on read
    # races any concurrent writer over the one record holding the slot and the vault
    # passphrase, and a torn write there leaves the workspace unopenable.
    instance = resolve(root=tmp_path, name="alpha")
    state = instance.root / "instance.json"
    before = state.stat().st_mtime_ns

    resolve(root=tmp_path, name="alpha")
    resolve(root=tmp_path, name="alpha", auth=False)  # same value as stored
    assert state.stat().st_mtime_ns == before

    resolve(root=tmp_path, name="alpha", auth=True)
    assert state.stat().st_mtime_ns != before


def test_the_state_file_is_not_world_readable(tmp_path):
    instance = resolve(root=tmp_path, name="alpha")
    state = instance.root / "instance.json"
    assert state.stat().st_mode & 0o077 == 0
    assert json.loads(state.read_text())["password"] == instance.password


def test_the_environment_never_names_the_operators_ports():
    # 8000 and 5173 are where the operator's own instance lives. No slot can reach them,
    # and this is the assertion that keeps the bases from drifting into range.
    for slot in range(ports.MAX_SLOTS):
        backend, frontend, stub = ports.slot_ports(slot)
        assert 8000 not in (backend, frontend, stub)
        assert 5173 not in (backend, frontend, stub)


def test_the_environment_isolates_every_piece_of_state(tmp_path):
    env = resolve(root=tmp_path, name="alpha").env()
    root = str(tmp_path / "alpha")
    assert env["HEPHAESTUS_DATA_DIR"].startswith(root)
    # Outside `data_dir` in the app too, and it has to be redirected on its own or code
    # mode cuts real git worktrees into the operator's ~/.hephaestus/worktrees.
    assert env["HEPHAESTUS_WORKTREES_DIR"].startswith(root)
    assert root in env["HEPHAESTUS_DB_URL"]
    assert env["HEPHAESTUS_OFFLINE_CHECK_ENABLED"] == "false"


def test_the_cors_origins_are_json_the_settings_layer_can_parse(tmp_path):
    # A list field, so pydantic-settings reads it as JSON. Getting this wrong blocks
    # every call the dev frontend makes, in the browser, with no server-side trace.
    instance = resolve(root=tmp_path, name="alpha")
    origins = json.loads(instance.env()["HEPHAESTUS_CORS_ORIGINS"])
    assert f"http://127.0.0.1:{instance.frontend_port}" in origins
    assert f"http://localhost:{instance.frontend_port}" in origins


def test_the_settings_layer_accepts_the_environment_verbatim(tmp_path, monkeypatch):
    # The end of the contract: every name above has to be one `Settings` actually reads,
    # and a typo in any of them would isolate nothing while looking like it did.
    from pydantic_settings import SettingsConfigDict

    from core.config import Settings as BaseAppSettings

    class Settings(BaseAppSettings):
        model_config = SettingsConfigDict(env_file=None)

    instance = resolve(root=tmp_path, name="alpha")
    for key, value in instance.env().items():
        if key.startswith("HEPHAESTUS_"):
            monkeypatch.setenv(key, value)
    settings = Settings()
    assert settings.port == instance.backend_port
    assert settings.data_dir == instance.data_dir
    assert settings.worktrees_dir == instance.worktrees_dir
    assert settings.auth_enabled is False
    assert settings.unlock_passphrase == instance.password
    assert settings.sandbox_enabled is False
    assert settings.cors_origins == [
        f"http://127.0.0.1:{instance.frontend_port}",
        f"http://localhost:{instance.frontend_port}",
    ]


def test_allocation_reports_the_remedy_when_every_slot_is_claimed():
    with pytest.raises(ports.NoFreeSlot) as excinfo:
        ports.allocate(set(range(ports.MAX_SLOTS)))
    assert "~/.hephaestus/dev" in str(excinfo.value)


def test_a_slot_whose_ports_are_in_use_is_skipped(monkeypatch):
    # The other half of allocation: a slot nobody has recorded but whose ports are held
    # by something on this host — another project's dev server, most likely.
    busy = set(ports.slot_ports(0))
    monkeypatch.setattr(ports, "is_free", lambda port: port not in busy)
    assert ports.allocate(set()) == 1


def test_seeding_is_recorded_against_a_version(tmp_path):
    instance = resolve(root=tmp_path, name="alpha")
    assert instance.seeded == ""
    instance.with_seeded("v7").save()
    assert resolve(root=tmp_path, name="alpha").seeded == "v7"


def test_ports_within_a_slot_stay_a_fixed_distance_apart(tmp_path):
    # One number identifies the triple. If these ever drift, a URL, a CORS origin and a
    # generated launch config start disagreeing about which instance they mean.
    instance = DevInstance(name="x", slot=3, root=tmp_path, password="p")
    assert instance.backend_port == ports.BACKEND_BASE + 3
    assert instance.frontend_port == ports.FRONTEND_BASE + 3
    assert instance.stub_port == ports.STUB_BASE + 3
