# 40 — Tools & Toolsets

> **Detail for Pillar III (the engine's reach).** This expands §2 / §3-pillar-III of the [architecture README](./README.md): how the agent's tools are defined, *which* tools a given run sees, and how the spec's whole access-control story (`AE-2`, `AE-3`) becomes **composition of Pydantic AI primitives keyed on per-run dependencies** rather than bespoke machinery.
---

## 1. The one idea

**A tool is a thin adapter; the catalog the model sees is a policy.** Two ideas, kept apart:

- **What a tool *is*** — a typed function the model can call. It owns *no* logic: it validates args (Pydantic AI does this), reaches a capability through `RunContext.deps`, and shapes the result. The actual work lives in `services/`, where a REST route reaches the same capability directly. A tool is the model's *doorway* to a capability, never the capability itself.
- **What the model *sees*** — not the raw catalog, but the catalog **after our access policy runs**: namespaced, operator-disabled tools dropped, sensitivity attached. That policy is a short **stack of toolset wrappers** evaluated against the run's deps. It is the single most leveraged mapping in the design.

Everything below is those two ideas worked out.

---

## 2. Tools are adapters over capabilities

A tool lives in `tools/`, grouped by `AE-2` category, and stays thin by construction:

```python
# tools/memory.py — the whole tool. No MEM-* logic here; it lives in services/memory.
@toolset.tool
async def recall(ctx: RunContext[RunDeps], query: str, limit: int = 5) -> list[dict]:
    """Recall relevant memories by meaning (with keyword fallback)."""
    store = ctx.deps.memory
    if store is None:                      # capability absent → say so, don't fail (XC-DEG-*)
        return [{"error": "Memory is unavailable."}]
    hits = await store.recall(ctx.deps.owner_id, query, limit=limit)
    return [{"content": h.memory.content, "matched_by": h.matched_by} for h in hits]
```

Three properties fall out of this shape and are non-negotiable:

| Property | Why it holds | Requirement |
|---|---|---|
| **Logic never hides in a tool.** | The `MemoryStore` is the one implementation; the tool, the `/memory/*` route, and any pipeline all call it. One source of truth, three callers. | README §3-pillar-III |
| **Capabilities degrade, tools report.** | A tool reads its capability handle off `deps`; `None` means the service isn't wired, and the tool returns a plain message the model adapts to — never an exception, never a hang. | `XC-DEG-*` |
| **Tools reach the run, not globals.** | `ctx.deps` carries the `Run`, the `owner_id`, and capability handles. A slow tool emits its own `tool.progress` (elapsed + partial output) through `deps.run`. Nothing is reached through module state. | `AE-6.1` |

### `RunDeps` — the keyed context every tool receives

`RunDeps` *is* the agent↔tools contract. It lives in `tools/deps.py` (below `agent/`, above `services/`, so both import it without a cycle) and becomes `RunContext.deps` inside Pydantic AI:

```python
@dataclass
class RunDeps:
    run: Run                                       # emit tool.progress; honor cancellation
    owner_id: str                                  # the ownership seam
    disabled_tools: frozenset[str] = frozenset()   # operator's enable/disable policy (AE-3.3)
    memory: MemoryStore | None = None              # capability handles — more land as services do
    sandbox: Sandbox | None = None                 # None ⇒ code-exec disabled, never host fallback
```

The deps object is assembled once per turn by the turn loop (`agent/turn.py`) and is the *only* thing the gating stack and the tools are keyed on. New capabilities land as new fields here, never as imports inside a tool.

---

## 3. The toolset stack — *which* tools a run sees

`AE-2` says tools are grouped into categories. `AE-3` says access is governed by **sensitivity** and by the operator's **enable/disable** choice. `AE-4` says the model picks its own tools from what it's offered. All three are realized by composing Pydantic AI toolset wrappers — `build_agent_toolsets()` is the whole policy:

```python
# tools/toolsets.py — the entire access policy, as built.
def build_agent_toolsets(categories=None, dormant=(), *, call_timeout_s=None):
    cats = dict(categories) if categories is not None else core_categories()
    prefixed  = [ts.prefixed(name) for name, ts in cats.items()]   # stable category_tool names
    combined  = CombinedToolset(prefixed)                          # one catalog, all categories
    stack = (combined.filtered(_enabled_gate)                      # drop operator-disabled tools
                     .prepared(_approval_gate)                     # mark what the level can't run
                     .prepared(_describe))                         # restate in catalog names
    return [RecoveringToolset(stack, call_timeout_s=call_timeout_s)]  # a raise is a result
```

Read as a pipeline, against the README's idealized stack:

```
CombinedToolset(builtin + memory + code + … MCP/integrations as they land)
  → .prefixed(category)        #              stable "category_tool" names           (AE-2.x)
  → .filtered(enabled_gate)    # AE-3.3       drop operator-disabled tools
  → .prepared(approval_gate)   #              mark every tool that reaches past this run's
  #                              permission level as `unapproved`, so Pydantic AI defers its
  #                              call to the engine's decision point instead of running it
  #                              (services/permissions). Only ever *adds* a gate: a tool that
  #                              defers on its own account keeps doing so at every level.
  → RecoveringToolset          #              an unexpected exception becomes a ToolFailed the
  #                              model reads, worded by core/diagnostics.py; each call holds
  #                              the run's keepalive under a per-call timeout
  # no privilege gate          — single operator, no tiers: there is no tier to filter on
  # no relevance pre-filter    — a capable native-tool-call model discerns from the full
  #                              gated catalog; AE-4.2 is trivially met (every tool always present).
  #                              A relevance step composes onto the same `.prepared()` seam if
  #                              the catalog ever outgrows what one prompt should carry.
```

Two deliberate omissions, each a settled decision rather than an oversight:

- **No privilege gate.** The spec's README §2 stack shows a `privilege_gate` first. With a single operator there are **no tiers**, so there is nothing to filter on — the line is intentionally absent. The `owner_id` seam carries the *future* multi-user story; when a second human exists, a `.filtered(privilege_gate)` slots in ahead of the enabled gate, keyed on the same deps. The shape is ready; the filter is empty today.
- **No relevance pre-filter.** `AE-4.1` (pre-select likely-relevant tools) is a waivable SHOULD-performance. On one powerful host with native-tool-call models, the model selects its own tools from the full gated catalog; pre-filtering would add latency and a place to be wrong. `AE-4.2` ("every permitted tool reachable") is then trivially satisfied. The `.prepared()` seam now carries the approval gate, and a relevance step composes onto it if/when the catalog grows past what one context should hold.

**Where the permission level acts, and why in two places.** A level narrows the catalog *and* marks what survives. The narrowing is the read-only level's alone (`services/tool_policy.py`): a tool that is not offered cannot be called by a model that decides otherwise, and a read-only turn saves the schemas outright. Every other level offers the full catalog and marks the tools past its write scope, because withholding one from them would tell the model the capability does not exist rather than that it needs permission. Both halves ask the same question — `services/permissions.beyond_scope` — so they cannot draw the line in different places.

### The category catalog

`default_categories()` is where the catalog grows — one entry per cluster, each a `FunctionToolset`:

| Category | Tools (today) | Backing capability | Status |
|---|---|---|---|
| `builtin` | `now` | — (starter category so the stack has something to compose) | ✅ built |
| `memory` | `remember`, `open`, `recall`, `list`, `audit`, `update`, `consolidate`, `forget` | `services/memory` (hybrid recall, `MEM-*`) | ✅ built |
| `platform` | `search`, `read` | `services/corpus` over every source, plus the skill, agent and plugin catalogs | ✅ built |
| `code` | `execute`, `request_egress`, `run_host_command` | `services/sandbox` + `services/egress` (`XC-SEC-7`) | ✅ built |
| `view` | `view_show` (file → version, or serve → live head), `view_close` | `services/artifacts` (version store) + `services/sandbox` (live head) | ✅ built |
| `web` | `search`, `fetch` | `services/search` + `services/searxng` (`SEARCH-*`) | ✅ built |
| `browse` | the harness's eighteen Playwright tools (`navigate`, `snapshot`, `click`, `type_text`, `screenshot`, `tabs`, …) | `services/browser` — a conversation-scoped browser context on web fetch's Chromium in the platform VM, with one login profile kept on the host; the operator watches and drives it through the app's live view | ✅ built |
| `document` | `create`, `edit` | `services/documents` (versioned library, `DOC-1`/`DOC-2`) | ✅ built |
| `attachments` | `provision` | `services/uploads` + `services/sandbox` (stage a file into `/work`) | ✅ built |
| `skills` | `open`, `create`, `edit` | `services/skills` (Agent Skills bundles, `SKILL-1..3`) | ✅ built |
| `plugins` | `write`, `read`, `test`, and each published plugin's tools as `plugins_<plugin>_<tool>` | `services/plugins`: drafts the operator publishes, run in the platform VM | ✅ built |
| `routines` | `write`, `list`, `delete`, `run_now` | `services/routines`: drafts the operator approves, each run on time in a new thread by its agent | ✅ built |
| `mail`, … | — | land with their `services/` capability | ⬜ pending |
| MCP servers, integrations | — | external (`MCP-*`, `INTEG-*`) | ⬜ pending (gating designed — §4.4) |

Naming is `category_tool` (the `.prefixed(name)` step), so `recall` is offered as `memory_recall` — stable, collision-free names the operator's enable/disable list and the event stream both reference.

### Progressive disclosure — how a large library stays three tools

The catalog is lean and unfiltered: no runtime relevance step decides which tools a turn sees. `skills` is where that constraint met its hardest case — a library of a hundred procedures — and the answer is the Agent Skills standard's own: **the catalog carries tools, not content.** The turn's instructions list the handful of platform skills that ship with the app, say how many other skills there are (stored, the project's, plugins'), and name only the few the operator pinned; the model finds the rest with `platform_search`, calls `skills_open` for the full text of the one it needs, and that call also stages the skill's bundled files into the sandbox so its scripts and references are reachable through `code_execute`. A thousand skills is still three tools and one fixed line.

---

## 4. Sensitivity & gating — the four-decision story

`AE-3` divides tools by **sensitivity**: powerful or hard-to-reverse capabilities (host execution, host fs-write, email-send, model serve/stop, config, vault, *and* externally-registered tools) are **approval-gated when the agent invokes them**. Crucially, **gating is not a filter in the stack above** — a sensitive tool is *present* in the catalog; it simply **pauses for the operator before it executes**. That pause is one mechanism that the sections below build on.

### 4.1 Approval via Pydantic AI deferred tools (the base mechanism)

A sensitive tool is marked `requires_approval=True` (or raises `ApprovalRequired` for *conditional* sensitivity, e.g. a write to a protected path). The flow, split clean down the chassis/engine line:

```
model requests a sensitive tool
  → Pydantic AI ends the turn with DeferredToolRequests   ← it did the agentic work,
      (tool name + VALIDATED args + tool_call_id)            and pointedly did NOT execute
  → WE park the Run: status → awaiting_input
  → WE emit  approval.required  (human-readable action + args [+ explanation])
  → WE notify on the right channel (inline | push/email for an unattended run)
  → WE hold the serialized message history as a ParkedTurn on the run
        … run survives client disconnect, indefinitely (AE-7) …
  → operator decides:  POST /runs/{id}/approve
  → WE resume with DeferredToolResults(approvals={id: ToolApproved() | ToolDenied(message)})
        ToolApproved may carry override_args; ToolDenied.message is shown to the model so it adapts
```

As built: the `Agent` is constructed in `agent/factory.py` with `output_type` `[str, DeferredToolRequests]`, so a turn either finishes with text or returns pending approvals. Inside `agent/turn.py`'s loop, `agent/gating.py`'s `settle_deferred()` rules on each deferred call (grant, then the thread's permission level, then Auto's review), and whatever is left for the operator goes to `agent/parking.py`'s `park_for_input()`, which stashes a `ParkedTurn` (the agent, the message history, the requests, any pending correction-drop range) on `run.parked_payload`; `build_resume_orchestrator()` consumes it and feeds the operator's decisions back in. **Pydantic AI decides *what* needs approval and *exactly what it would do*; we own *parking, notifying, waiting, resuming*.** Because the parked state is just serialized history + pending requests, it survives disconnect for free and can outlive the connection entirely for unattended runs.

> The frozen v1 event protocol carries `approval.required`, with an **optional `explanation`** field for the host-exec case — additive, no version bump.

### 4.2 Code execution is a blast radius, not merely a gate

Approval is **consent, not containment**: a misjudged click, a destructive command dressed up as benign, or injection that *manufactures* a plausible approval request all land on the real host if approval is the only line. So code execution gets a structural boundary *underneath* the gate. The boundary is not the walls around a running command — those only made ordinary work (`pip install`, a build cache, a scratch file outside `/work`) feel like an attack on the system. It is the small set of **exits** a workspace has, everything inside being disposable by construction. This is why `code` is **three tools** (`tools/code.py`), of which exactly two are exits:

| | `execute` | `request_egress` | `run_host_command` |
|---|---|---|---|
| Runs on | the conversation's **sandbox** in the platform VM (`services/sandbox`, `services/vm`) | — (widens the workspace's allowlist) | the **real host** |
| Approval | **none** — compute is not an exit | **`requires_approval=True`**, **per call, never a standing grant** | **`requires_approval=True`** (deferred) |
| Extra contract | — | `domains` + a `reason` in the model's own words; the domain is remembered for the workspace once approved, the *act of widening* never is | a plain-language **`explanation`** arg, shown to the operator, describing what it does and its effect on the host (`AE-3.4`) |
| If capability absent | returns "code execution is unavailable" — the model adapts | reports no allowlist is configured | n/a |
| In a sub-agent's run | available | **withheld** — a child has nobody to ask | **withheld** |
| In a thread filed under a project | available — it runs in the thread's scratch, and the shell works on the project | available | available |

The inversion is the point: **routine code-exec has no approval friction at all**, and the approval prompts are the genuine exits — changing the operator's host, and reaching somewhere nobody approved.

**The isolation invariant** (`services/sandbox/base.py`), as it now stands:

| Invariant | Status | How |
|---|---|---|
| Real files enter as **copies** | unchanged | the workspace is a host-side dir piped into the VM per run as `/work` (nothing is mounted); a coding thread gets a worktree on a throwaway branch instead. The operator's own tree is written only by a merge they press |
| No host filesystem / process / environment reach | unchanged | each command runs under bubblewrap as uid 1100 inside the platform VM, only `spec.env` passed; the data dir, vault, uploads and `$HOME` never enter the VM |
| Walls inside the box | **relaxed** | the box is not root, so a read-only root bought nothing the unprivileged user did not; caps are the `sandbox_memory` / `sandbox_cpus` / `sandbox_pids_limit` settings. Compute and installing packages must not feel restricted |
| Network | **reshaped** | not "off by default" and no per-call `network=True` switch. A sandbox command has no network except through the VM's **allowlisting egress gateway** on the host (`services/vm/egress.py`). One list feeds both fences, not one per fence: the gateway reads `egress_allowed_domains` plus whatever `code_request_egress` widened for *that* workspace (`services/egress.py`) |
| Execution outside the VM | **fenced** | the approved host command runs under seatbelt/bubblewrap confinement (`host.py`) reading that **same** `egress_allowed_domains` — credential paths and the data dir unreadable. It degrades with `confined:false` where confinement cannot initialise, since the operator approved *this* command. Shell commands do not run here: they run in the platform VM, in the agent's home (`services/sandbox/home_shell.py`); only one declared `host` runs on the operator's machine, unfenced, after approval |
| A sub-agent's workspace | **shared, or a worktree of its own on request** | it works in the launching thread's files by default, with the parent's write access. `worktree` gives it its own branch and worktree of the parent's project, sharing the parent's scratch; its work waits there until the parent reviews it and calls `agents_merge` (a git merge into the parent's worktree, or file changes on the checkout that can be put back) or `agents_discard`. A definition declaring `isolated` always gets one |
| Fails closed, never to host | unchanged | with the platform VM disabled (`vm_enabled`) no `SandboxSessionManager` is registered in the capability bag, and `code_execute` reports the capability unavailable (`XC-DEG-*`) |

Code runs in one shared platform microVM (`services/vm`, smolvm/QEMU), which also hosts SearXNG and web fetch's headless Chromium. Outputs return explicitly (stdout/stderr + copied-out files); nothing escapes as a side effect. The operator's own terminal (`SHELL-*`) is unchanged and **agent-unreachable** — the agent's paths to the host are the explained-approval tool and, in a code thread, the fenced shell.

### 4.3 Pre-authorized scheduled tasks ⬜ *designed, not built*

Strict per-run approval (`AE-3.2`) would make a recurring task that touches a sensitive action **park every single run** — defeating unattended automation. Resolution: **the scheduling tool is itself a deferred/approval tool**. When the agent schedules a task, the Run parks and surfaces *the task* — its trigger, intended actions, and the specific sensitive actions it would perform — and approval grants a **scoped pre-authorization** stored with the task.

- **At each unattended run:** a sensitive action **within** the pre-authorized scope is auto-satisfied (the deferred result is supplied as approved) without re-parking; an action **outside** scope falls back to pause-and-notify (`AE-3.2`).
- **Scope, not a blank cheque:** the grant is over a *declared scope* (`may send email to these recipients`, `may run sandboxed code`, `may not run host code`), checked at runtime. Free-form tasks approve the *bounds*; runtime actions are matched against them.
- **Revocable & visible:** the operator can review and revoke any task's pre-authorization, returning it to strict per-run approval. Reuses the scheduler (which materializes tasks as Runs) and deferred-tool approval — no new machinery.

Lands with `TASK-*`.

### 4.4 External tools (MCP / integrations) sensitive by default ⬜ *designed, not built*

The `AE-3` sensitivity model rests on a *known* list (shell, email, vault…). Tools from registered MCP servers and configured integrations are the **unknown** case it can't enumerate — arbitrary, possibly externally-visible effects. So they are **approval-gated by default**, and the operator opts *specific* tools into **trusted** (auto-approve) status:

- In the toolset stack, external tools carry a default *sensitive* marking and ride the same deferred-tool path as the other sensitive tools.
- A **trusted-tool allowlist** (operator-managed, persisted in encrypted settings) flips specific tools to auto-approve.
- **Enable/disable (`AE-3.3`) is orthogonal and still applies** — the `_enabled_gate` runs regardless of trust.

Consistent with the whole posture: unknowns are contained until the operator deliberately, per-tool, relaxes them — never the agent. Lands with `MCP-*` / `INTEG-*`.

### 4.5 The Auto level's review — deterministic first, then a model

Three of the four permission levels answer a deferred call without help: Plan refuses it, Manual and Edit put it in front of the operator. **Auto is the level whose entire meaning is that the operator's answers are given for them**, so it needs something to give them — a review, in two stages, deterministic first (`services/permissions/`).

```
a deferred call at the Auto level
  → capability.py    what this call would do at its WORST, extracted from its arguments
  │                    a shell command: its programs, paths, env writes, network reach
  │                    (shell_ast.py walks the real bash grammar via tree-sitter-bash)
  │                  a file write: the one resolved target path
  │                  an MCP call: the connector, the tool, and the argument KEYS
  → judge.py         a STRUCTURAL stage over that capability — no list of programs.
  │                    Three facts have to agree: what the syntax names, the `reach` the
  │                    model DECLARED on the call, and whether a fence (the platform VM)
  │                    holds it there.  Contained +
  │                    declared + fenced clears at that tier.  Its only power is to
  │                    APPROVE; everything else escalates rather than being refused —
  │                    a `host` declaration, a `network` declaration (always, whatever
  │                    the operator's allowed-domains list holds: the fence bounds no
  │                    reads of the project, so egress is how what a command read leaves the machine),
  │                    a path outside the worktree, an AST shape it could not read, or
  │                    a command with no fence to hold it.
  → reviewer.py      one structured call on the `utility` model, scoring three axes:
  │                    risk           low | high | too_destructive
  │                    authorization  explicitly_no | neutral | explicitly_yes
  │                    correctness    a sentence, or null
  → decide.py        too_destructive PARKS — the operator is asked, never refused for
                     them.  low runs unless the operator said no.
                     high runs only on an explicit yes.  Everything else parks.
```

Four properties are load-bearing and each is there for a reason that is easy to lose:

- **The cheap stage settles the common case.** `git status`, `ls src`, `grep -rn foo .`, `uv run pytest`, `git commit -m x` clear with no model call at all — on their *shape*, whatever binary they happen to name. That is what makes the stage affordable to keep strict: narrowing it costs latency, widening it costs the operator's trust, and only one of those is recoverable.
- **The fence bounds writes and egress, and deliberately not reads of the project.** A cleared command runs in the VM against a copy of the project, so it can still read any of it by a route its words did not name. Containment is therefore checked in the judge before anything else, and the extraction refuses any word it cannot place as a single path — a program named by path and an environment assignment's value included.
- **The reviewer is told the rubric and never the passing score.** The combination lives in `decide.py` and is written down nowhere the model can read it. A reviewer that knows the threshold optimises for the threshold, which turns three independent observations into one negotiated verdict.
- **Its transcript carries user and assistant messages only — never tool results.** Everything the model has read from a file, a page or an MCP server is content someone else wrote, and a reviewer that saw it could be argued into approving the very call that untrusted text asked for. Same posture `XC-SEC-5` takes everywhere else, applied to the one call whose output is a permission. What it *does* see is fenced as untrusted on top of that.
- **Every failure parks.** No utility model bound, a reviewer timeout, an unparseable answer — all of them return the call to the operator. This is the one place in the codebase where the conservative branch has to be the default (`XC-DEG-*`): a review that cannot run is not a review that passes.

**And it is visible.** `review.started` / `review.completed` (additive to the frozen v1 protocol, no bump) carry the extracted worst case, the verdict, which stage settled it, and the three axes — rendered in the work log beside the call they judged. A level that answers for the operator is only worth having if they can read afterwards what was decided and why; without that, a tool call they never approved is indistinguishable from a gate that failed open.

---

## 5. How it all composes (one run)

1. The turn loop assembles `RunDeps` (run, owner, disabled-tool set, capability handles) — `agent/turn.py:drive_turn`.
2. `build_agent_toolsets()` produces the gated, namespaced stack; the `Agent` is built with it (`deps_type=RunDeps`, `output_type=[str, DeferredToolRequests]`).
3. The model runs its multi-step loop; for each call, the `_enabled_gate` and tool args are evaluated against `ctx.deps`. A non-sensitive tool executes and may emit `tool.progress`.
4. A **sensitive** tool does *not* execute — the turn ends with `DeferredToolRequests`. The engine rules on each deferred call: the thread's permission level refuses or parks it, a standing conversation grant answers the levels that would have asked, and at the Auto level the review settles it — with the grant riding into that review as the operator's authorization rather than skipping it (§4.5). What is still unanswered parks (§4.1) and waits for `POST …/approve`.
5. `code_execute` runs in the conversation's sandbox if the platform VM is up, else reports disabled (§4.2); `code_request_egress` and `code_run_host_command` always park for approval first — the two exits, never the compute.

The result: the spec's entire access-control surface (`AE-2` categories, `AE-3` sensitivity + enable/disable, `AE-4` model-discerns) is a **dozen lines of toolset composition plus a per-tool `requires_approval` flag** — keyed on one deps object, with every harder case (host exec, scheduled tasks, external tools) reusing the *same* deferred-tool pause rather than inventing new control flow.

---

## 6. Status & open seams

| Concern | State |
|---|---|
| Toolset stack (namespacing + enabled gate + approval gate) | ✅ built (`tools/toolsets.py`) |
| `builtin`, `memory`, `code` categories | ✅ built |
| Approval pause/resume (engine + `/runs/{id}/approve`) | ✅ built |
| Auto-level review (shell judge + utility-model reviewer) | ✅ built (`services/permissions/`, `agent/gating.py`) |
| Sandbox isolation + host escape hatch | ✅ built (`services/sandbox`, `tools/code.py`) |
| Blast-radius model (egress allowlist + VM gateway, fenced shell, forked delegates) | ✅ built (`services/egress.py`, `services/vm/egress.py`, `services/sandbox/{host,fork}.py`) |
| Async sub-agents (`subagents` category, register, wake, panel) | ✅ built (`services/subagents/`, `services/subagent_store.py`, `harness/manifests/subagents.py`, `tools/subagents.py`) — replaces the `agents` category and its blocking `delegate_task`, and the `research` category with it: research is now the `researcher` spec in the roster, launched, redirected (`subagents_send`) and listed (`subagents_list`) like any other |
| Agent definitions (`agents` category, catalog, per-thread agent, tool-access gate) | ✅ built (`services/agents/`, `harness/manifests/agents.py`, `tools/agents.py`, `routes/agents.py`) — the roster's specs became `AgentDefinition`s; the enabled gate also checks `RunDeps.tool_access` |
| Agent trees (`agents_spawn`/`message`/`list`/`read`/`stop`, depth, tree messaging, open-as-thread) | ✅ built (`harness/manifests/_subagents.py`, `_subagent_wake.py`, `tools/delegation.py`, `routes/subagents.py`) — the `subagents` category folded into `agents`; `tools/subagents.py` is gone |
| Version history (skills, agents, plugins: history, diff, restore) | ✅ built (`services/revisions.py`, `routes/revisions.py`) |
| Plugins (drafts, VM runtime, declared egress, secret placeholders) | ✅ built (`services/plugins/`, `services/vm/intercept.py`, `tools/plugins.py`, `routes/plugins.py`) |
| Routines (agent per routine, on-time runs in their own threads, time zones, agent drafts, natural-language schedules) | ✅ built (`services/routines/`, `tools/routines.py`, `routes/tasks.py`) |
| Privilege gate | 🔭 seam reserved — empty until a second user exists |
| Relevance pre-filter | 🔭 seam reserved — deliberately omitted |
| Scheduled-task pre-authorization | ⬜ designed, lands with `TASK-*` |
| External-tool gating + trusted allowlist | ⬜ designed, lands with `MCP-*` / `INTEG-*` |
| `search` / `mail` / `documents` / … categories | ⬜ land with their `services/` capability |

→ related detail: [`30-agent-engine.md`](./30-agent-engine.md) (the meta-loop and event translation), [`50-capabilities.md`](./50-capabilities.md) (the `services/` implementations tools adapt over).
