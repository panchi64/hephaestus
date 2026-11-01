# Harness issues backlog

Generated from `backlog.json` by `python -m evals.harness.issues`; edit status with
`issues set`, not by hand.

## Open

| Id | Severity | Kind | Area | Issue | Seen | Suspect |
| --- | --- | --- | --- | --- | --- | --- |
| H-001 | minor | platform | tooling | dev_instance status reports not ready when it runs on a real model | ×1, planning (2026-11-01) → planning (2026-11-01) | devkit/cli.py status_payload |
| H-002 | minor | behavior | intercom | The worker roster line says a running worker can't be corrected, but agents_message reaches it | ×1, planning (2026-11-01) → planning (2026-11-01) | services/agents/builtin.py |
| H-003 | minor | behavior | delegation | The delegation skill both says spawning a writer asks the operator and that spawning never asks | ×1, planning (2026-11-01) → planning (2026-11-01) | skills/delegation/SKILL.md |
| H-004 | minor | platform | tools | The VM image has no pytest, so every Python test run starts with an install | ×1, planning (2026-11-01) → planning (2026-11-01) | vm/build_image.sh |

- **H-001**: With HEPH_DEV_CHAT_* set the launcher never starts the stub (devkit/launch.py ensure_stub), but status_payload's `ready` is all(running.values()), which includes the stub port, so status --json says ready: false on a working instance.
- **H-002**: services/agents/builtin.py worker description: "you cannot correct a worker while it runs"; tools/delegation.py agents_message: "It reaches a running agent at its next step". Talos is told both, and the intercom scenarios depend on the second.
- **H-003**: skills/delegation/SKILL.md:18 "spawning one that can write asks the operator first" against :39 "Spawning and messaging never ask the operator".
- **H-004**: vm/build_image.sh installs python3-pip and python3-venv but not pytest; a sandboxed `pytest` fails with command not found until the agent pip-installs it.
