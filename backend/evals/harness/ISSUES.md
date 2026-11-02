# Harness issues backlog

Generated from `backlog.json` by `python -m evals.harness.issues`; edit status with
`issues set`, not by hand.

## Open

| Id | Severity | Kind | Area | Issue | Seen | Suspect |
| --- | --- | --- | --- | --- | --- | --- |
| H-004 | minor | platform | tools | The VM image has no pytest, so every Python test run starts with an install | ×1, planning (2026-11-01) → planning (2026-11-01) | vm/build_image.sh |

- **H-004**: vm/build_image.sh installs python3-pip and python3-venv but not pytest; a sandboxed `pytest` fails with command not found until the agent pip-installs it. *Note: Added to vm/build_image.sh; fixed once the image is rebuilt.*

## Fixed

| Id | Severity | Kind | Area | Issue | Seen | Suspect |
| --- | --- | --- | --- | --- | --- | --- |
| H-005 | major | behavior | delegation | Explicit request for a test runner ignored; Talos ran the suite itself | ×1, 2026-11-01-baseline (2026-11-01) → 2026-11-01-baseline (2026-11-01) | agents_spawn |
| H-006 | major | behavior | solving | Fix-the-tests request also flips low_stock from `<` to `<=` | ×1, 2026-11-01-baseline (2026-11-01) → 2026-11-01-baseline (2026-11-01) |  |
| H-001 | minor | platform | tooling | dev_instance status reports not ready when it runs on a real model | ×1, planning (2026-11-01) → planning (2026-11-01) | devkit/cli.py status_payload |
| H-002 | minor | behavior | intercom | The worker roster line says a running worker can't be corrected, but agents_message reaches it | ×1, planning (2026-11-01) → planning (2026-11-01) | services/agents/builtin.py |
| H-003 | minor | behavior | delegation | The delegation skill both says spawning a writer asks the operator and that spawning never asks | ×1, planning (2026-11-01) → planning (2026-11-01) | skills/delegation/SKILL.md |
| H-007 | minor | platform | delegation | Worker in a worktree cannot commit: its .git pointer is a host path and the git dir is read-only in the VM | ×1, 2026-11-01-baseline (2026-11-01) → 2026-11-01-baseline (2026-11-01) | agents_spawn |
| H-008 | minor | platform | delegation | agents_merge copies __pycache__ bytecode into the project | ×1, 2026-11-01-baseline (2026-11-01) → 2026-11-01-baseline (2026-11-01) | agents_merge |
| H-009 | minor | ux | delegation | agents_merge refuses a worker that has already reported blocked, naming a report as the remedy | ×1, 2026-11-01-baseline (2026-11-01) → 2026-11-01-baseline (2026-11-01) | agents_merge |
| H-010 | minor | behavior | flow | Edits parse_line after approval though no plan step covered it | ×1, 2026-11-01-baseline (2026-11-01) → 2026-11-01-baseline (2026-11-01) |  |
| H-011 | minor | eval | flow | Expect item wants a what-to-change question after a refusal that says to leave it as it is | ×1, 2026-11-01-baseline (2026-11-01) → 2026-11-01-baseline (2026-11-01) |  |
| H-012 | minor | eval | flow | Line-count check misreads a correct table answer | ×1, 2026-11-01-baseline (2026-11-01) → 2026-11-01-baseline (2026-11-01) |  |
| H-013 | minor | behavior | flow | Reply says the steps are described but describes none | ×1, 2026-11-01-baseline (2026-11-01) → 2026-11-01-baseline (2026-11-01) |  |
| H-014 | minor | eval | intercom | Scenario wants a blocked report, but the operator's wording steers Talos to message the worker | ×1, 2026-11-01-baseline (2026-11-01) → 2026-11-01-baseline (2026-11-01) |  |
| H-015 | minor | behavior | main | Asks an unrequested README-versus-code decision in prose | ×1, 2026-11-01-baseline (2026-11-01) → 2026-11-01-baseline (2026-11-01) | builtin_ask_user |
| H-016 | minor | behavior | main | No task list for a four-step job | ×1, 2026-11-01-baseline (2026-11-01) → 2026-11-01-baseline (2026-11-01) | tasks_write |
| H-017 | minor | behavior | models | Asks the operator how to proceed after agents_models says to spawn without a model | ×1, 2026-11-01-baseline (2026-11-01) → 2026-11-01-baseline (2026-11-01) | builtin_ask_user |
| H-018 | minor | behavior | subagent | Single-question research skips the plan step the research skill requires | ×1, 2026-11-01-baseline (2026-11-01) → 2026-11-01-baseline (2026-11-01) | skills_open |
| H-019 | minor | behavior | subagent | Worker not spawned after Talos finds the file missing | ×1, 2026-11-01-baseline (2026-11-01) → 2026-11-01-baseline (2026-11-01) |  |
| H-020 | minor | behavior | tools | Cites a python.org page it never opened | ×1, 2026-11-01-baseline (2026-11-01) → 2026-11-01-baseline (2026-11-01) |  |
| H-021 | polish | behavior | delegation | Closes a read-only review with an offer to fix | ×1, 2026-11-01-baseline (2026-11-01) → 2026-11-01-baseline (2026-11-01) |  |
| H-022 | polish | behavior | delegation | Final reply ends on an unrequested README-versus-code question | ×1, 2026-11-01-baseline (2026-11-01) → 2026-11-01-baseline (2026-11-01) |  |
| H-023 | polish | behavior | delegation | Stop confirmation runs two sentences together and repeats itself | ×1, 2026-11-01-baseline (2026-11-01) → 2026-11-01-baseline (2026-11-01) |  |
| H-024 | polish | ux | flow | Approved plan's task ids are not given to the model; first status update fails | ×1, 2026-11-01-baseline (2026-11-01) → 2026-11-01-baseline (2026-11-01) | plan_submit |
| H-025 | polish | behavior | flow | Parallel file writes into a directory that does not exist | ×1, 2026-11-01-baseline (2026-11-01) → 2026-11-01-baseline (2026-11-01) | files_write_file |
| H-026 | polish | behavior | main | Extra lookups after memory_recall already answered | ×1, 2026-11-01-baseline (2026-11-01) → 2026-11-01-baseline (2026-11-01) |  |
| H-027 | polish | platform | models | subagent_report accepted twice in one turn | ×1, 2026-11-01-baseline (2026-11-01) → 2026-11-01-baseline (2026-11-01) | subagent_report |
| H-028 | polish | behavior | subagent | Final reply ends on an unrequested README-versus-code question | ×1, 2026-11-01-baseline (2026-11-01) → 2026-11-01-baseline (2026-11-01) |  |

- **H-005**: operator: "Have a test runner run the test suite"; calls were `files_edit_file`, then `shell_start_command` "python -m pytest -v"; no `agents_spawn` *Note: Instructions changed; confirm on the next run.*
- **H-006**: "It used `<`, but the README says 'at or below the threshold'. I changed it to `<=` and added a test... This changes behavior for any caller relying on the old strict comparison." *Note: Instructions changed; confirm on the next run.*
- **H-001**: With HEPH_DEV_CHAT_* set the launcher never starts the stub (devkit/launch.py ensure_stub), but status_payload's `ready` is all(running.values()), which includes the stub port, so status --json says ready: false on a working instance. *Note: Fixed in code, with a test.*
- **H-002**: services/agents/builtin.py worker description: "you cannot correct a worker while it runs"; tools/delegation.py agents_message: "It reaches a running agent at its next step". Talos is told both, and the intercom scenarios depend on the second. *Note: Worker description corrected.*
- **H-003**: skills/delegation/SKILL.md:18 "spawning one that can write asks the operator first" against :39 "Spawning and messaging never ask the operator". *Note: Delegation skill corrected.*
- **H-007**: "fatal: not a git repository: ~/.hephaestus/dev/odysseus/data/harness-fixtures/..." and "Unable to create '.../index.lock': Read-only file system" *Note: Fixed in code, with a test.*
- **H-008**: merge files: ["README.md", "__pycache__/inventory.cpython-312.pyc", "inventory.py", "tests/__pycache__/...pyc", "tests/test_inventory.py"] *Note: Fixed in code, with a test.*
- **H-009**: "`inventory-remove-fn` is still working in its worktree / help: wait for its report, or stop it with agents_stop first" after the report had arrived; `agents_stop` returned {"stopped": []} *Note: Fixed in code, with a test.*
- **H-010**: plan steps: add remove, two tests, README, run suite; then `files_edit_file` int(float(price) * 100) -> round(float(price) * 100) *Note: Instructions changed; confirm on the next run.*
- **H-011**: refusal: "Declined. Leave it as it is."; expect 2: "Asks what to change about the plan rather than doing it anyway." *Note: Scenario corrected.*
- **H-012**: table `| inventory.py | 5 | 37 |` with a Lines column fails check "answer matches /\b\d+\s+lines?\b/" *Note: Scenario corrected.*
- **H-013**: "All three steps are described and marked complete on the task list." *Note: Instructions changed; confirm on the next run.*
- **H-014**: operator: "tell it to ask you for it"; brief: "Ask your parent ... with the agents_message tool"; check "`subagent_report*` calls with blocked" failed *Note: Scenario corrected.*
- **H-015**: "Do you want the code changed to `<=` to match the README, or the README changed to match the code?" *Note: Instructions changed; confirm on the next run.*
- **H-016**: docstrings, test run, diagnosis and report with no `tasks_write` call; check "a task list of >=2, all closed" failed *Note: Instructions changed; confirm on the next run.*
- **H-017**: `builtin_ask_user` -> "A: (none of the options) Use your best judgement and carry on." *Note: Instructions changed; confirm on the next run.*
- **H-018**: `skills_open` research, then `agents_spawn` researcher directly; no `plan_enter` or `plan_submit` *Note: Research skill now exempts a single factual question from plan mode.*
- **H-019**: "`.../secrets/prod.env` doesn't exist, so I didn't spawn a worker." *Note: Instructions changed; confirm on the next run.*
- **H-020**: "The [source releases page](https://www.python.org/downloads/source/) also names 3.15.2"; the only `web_fetch` was https://www.python.org/downloads/ *Note: Instructions changed; confirm on the next run.*
- **H-021**: "Do you want me to fix these, starting with the price parsing?" *Note: Instructions changed; confirm on the next run.*
- **H-022**: "I haven't changed anything. Which `low_stock` behavior is intended, the README's or the code's?" *Note: Instructions changed; confirm on the next run.*
- **H-023**: "nothing in the project was changed.The test runner's final report confirms the stop... Nothing further is needed." *Note: The eval joined text segments split by a steer with no separator; fixed in the timeline.*
- **H-024**: `tasks_update_statuses` {"task_id": "1"} -> "Step with id '1' not found"; `tasks_read` shows [t1]..[t4] *Note: Fixed in code, with a test.*
- **H-025**: "Parent directory 'projects/inventory-7b206d/summaries' does not exist. Create the parent directory first." (3 of 4 writes) *Note: Fixed in code, with a test.*
- **H-026**: `memory_recall` returned port 6544, then `project_active` and `platform_search` ran before the reply *Note: Instructions changed; confirm on the next run.*
- **H-027**: two identical `subagent_report` calls, both {"filed": true, "to": "main"} *Note: Fixed in code, with a test.*
- **H-028**: "I left it alone. Which should change, the code or the README?" *Note: Instructions changed; confirm on the next run.*
