# Hephaestus — Backend Architecture

> **Status: living design.** How the backend is built: structure, abstractions and seams.
>
> The spec that seeded this design is retired. Requirement ids (`AE-*`, `XC-*`, …) still cited here and in the code are historical; where one disagrees with the code, the code is right. This document and the capabilities of **FastAPI** + **Pydantic AI** are authoritative.
>
> The design derives from the deployment profile (single operator, one powerful local host, models with native tool-calling) and those two libraries.

---

## 1. The one idea to hold in your head

The system separates cleanly into two halves, and almost every design choice falls out of respecting that line:

```
  ┌─────────────────────────────────────────────────────────────┐
  │  INFRASTRUCTURE WE OWN                                        │
  │  run lifecycle · queueing · the event stream · disconnect    │
  │  survival & resume · cancellation · timeouts · persistence · │
  │  access policy · the verifier/loop-break meta-loop           │
  │                                                              │
  │     ┌────────────────────────────────────────────────┐      │
  │     │  AGENTIC REASONING — Pydantic AI owns this       │      │
  │     │  the model call · tool selection by the model ·  │      │
  │     │  typed-arg validation · the within-turn          │      │
  │     │  tool→observe→continue loop · per-tool retries · │      │
  │     │  model fallback · output validation · history    │      │
  │     │  processing                                      │      │
  │     └────────────────────────────────────────────────┘      │
  └─────────────────────────────────────────────────────────────┘
```

**Pydantic AI is the engine; we are the chassis.** We never re-implement what Pydantic AI does well (the loop, validation, fallback, streaming primitives). We own everything that turns one model run into a durable, observable, resumable product feature, which is not the library's job.

In short: **all agentic logic goes through Pydantic AI; all orchestration, transport and state around it is ours.**

---

## 2. The three pillars

Everything is built on three layers. Read them in this order.

### Pillar I — The Run substrate (the chassis)

A **Run** is the central abstraction: one server-side, identified, background-executing unit of work for one user request. Chat turns, sub-agent turns and routine runs are all Runs — they differ only in which *orchestrator* drives them, not in how they are launched, streamed, observed, resumed, or cancelled.

A Run owns:

- an **id, owner, and status** state machine (`queued → running → {done | blocked | error | cancelled}`, with a parked `awaiting_input` state when a sensitive tool call needs approval),
- a **typed, sequence-numbered event stream** (Pillar II) published to an in-process broker,
- an **in-memory event buffer** so a reconnecting client can replay what it missed (`AE-7`); it lives only as long as the server process,
- a background **asyncio task** tracked in a `RunRegistry`, decoupled from any client connection: closing the browser does not stop the work (`AE-7.1`),
- **bounds**: a max-step ceiling (`AE-1.5`), optional tool-call ceiling (`AE-1.6`), an inactivity watchdog and an opt-in wall-clock limit (`XC-PERF-2`), and cancellation that takes effect at the next step boundary (`DR-3.3`, `CHAT-5`). The step ceiling and the watchdog apply to every run; the wall clock is off unless the deploy or the operator sets one, since a turn that is merely slow is a turn we want to let finish.
- **a lane**: concurrency is bounded per lane rather than by one shared pool — `interactive` (the operator's own turn), `background` (a scheduled task), `linked` (a thread the agent opened for itself) — picked from the run's `kind`, so unattended work can never leave a turn somebody is waiting on queued at the gate,
- **an endpoint lease**: past its lane, a run waits in line for its model endpoint's capacity and holds it for the whole turn (`runs/endpoint_gate.py`), announcing its place with `run.queued`.

Because every long-running feature is a Run, continuity/resume/cancel/timeout/metrics are written **once** and inherited everywhere.

→ detail: [`10-run-substrate.md`](./10-run-substrate.md)

### Pillar II — The event protocol (the contract)

`AE-6` enumerates what must stream to the client: incremental answer text; reasoning kept *separate* from the answer (`AE-6.3`); each tool's start/progress/result; live progress for slow tools (elapsed + partial output); step boundaries; document create/stream/commit (`AE-6.2`); cited web sources; budget-limit notices; final run metrics; errors; and an explicit end-of-turn. We add one event the spec implies via gating: an **`approval_required`** event (the pending sensitive action + its validated args), paired with the `awaiting_input` run state and a `POST …/approve` control.

We model this as **one versioned, typed union of events** (Pydantic models), each carrying a monotonic `seq` for `Last-Event-ID` resume. This union *is* the backend↔frontend contract — it replaces any ad-hoc shapes. **The v1 set is frozen:** `run.*`, `step.*`, `thinking.delta`/`answer.delta`, `tool.*`, `document.*`, and the `citation.added`/`approval.required`/`limit.notice` notices — named `entity.event`, dot.lowercase, past-tense for discrete events and `delta`/`progress` for streams. The agent engine produces it by **translating Pydantic AI's native stream** (`PartStartEvent`/`PartDeltaEvent` for text and thinking parts, tool-call and tool-return events, `FinalResultEvent`) into our domain events, and by emitting the events Pydantic AI knows nothing about (step boundaries, document lifecycle, budget notices, run metrics, end-of-turn).

→ detail: [`20-event-protocol.md`](./20-event-protocol.md)

### Pillar III — The agent engine, tools, and capabilities (the engine + its reach)

Three nested concerns:

1. **The agent engine** wraps Pydantic AI's `Agent`. Within a turn we let the `Agent` run its multi-step loop (driven via `agent.iter()` so we can observe each graph node and stream it). *Around* that we own the **meta-loop**: the post-turn verifier that checks each promised deliverable actually exists (`AE-1.4`, `AE-5.2`) and, if not, makes a *bounded* corrective re-attempt (`AE-5.5`); and the loop-breaker that aborts on no-progress repetition (`AE-5.1`). Context reduction near the model's limit (`AE-5.4`, `CHAT-4`) is a Pydantic AI history processor. Model fallback (`AE-5.3`) is `FallbackModel`.

2. **Tools** are exposed to the model as Pydantic AI toolsets, but *which* tools a given run sees is **our policy**, expressed as a stack of toolset wrappers evaluated against the run's `RunContext` deps:

   ```
   CombinedToolset(all categories + MCP servers + integrations)
     → .filtered(privilege_gate)     # AE-3.1 / AE-3.2  drop tools above the user's tier
     → .filtered(enabled_gate)       # AE-3.3           drop user-disabled tools
     → .renamed(namespacing)         #                  stable "category.tool" names
     # (no relevance pre-filter: the model discerns from the full gated catalog;
     #  a .prepared() relevance step can slot in here later if the catalog ever grows too large)
   ```

   Access control and namespacing are *composition of library primitives keyed on per-run dependencies*, not bespoke machinery. Relevance pre-filtering (`AE-4.1`, a waivable SHOULD) is deliberately omitted: capable native-tool-call models on one powerful host select their own tools, and `AE-4.2` is met because every tool is always present.

3. **Capabilities** (`services/`) are the actual implementations — web search, vector store, memory, embeddings, the model registry, mail — each an async interface with a graceful-degradation story (`XC-DEG-*`). **Tools are thin adapters over capabilities.** The same capability is reused by a tool (agent calls it) and by a plain REST route (user calls it directly). Logic never hides inside a tool.

→ details: [`30-agent-engine.md`](./30-agent-engine.md), [`40-tools-and-toolsets.md`](./40-tools-and-toolsets.md), [`50-capabilities.md`](./50-capabilities.md)

---

## 3. Cross-cutting foundations

- **Platform-agnostic by construction.** The backend MUST run on Linux, macOS, and other POSIX hosts with no OS-specific dependency. Concretely: **no OS keystore** (the encryption key is password-derived and memory-only), filesystem access via `pathlib` not hard-coded separators, no shelling out to platform-only binaries in the core, and all crypto/storage deps chosen for cross-platform wheels (SQLCipher, `argon2-cffi`, pyca `cryptography`, sqlite-vec). Model inference is an HTTP call to a local OpenAI-compatible server, which is itself OS-portable. Any genuinely platform-specific capability (if one ever appears) must sit behind an interface with a portable default.
- **Topology — origin-agnostic.** The backend is a pure API: **bearer-token** auth (works same-origin or split-origin), CORS configurable, no assumption about who serves the SPA. A deployment may put the API and the built frontend behind one reverse proxy or run them apart; the backend does not care.
- **Security posture (single-operator).** Auth is enforced **globally** by middleware before any feature is reached (`XC-SEC-1`). The spec's "privilege" is split into three axes: **(A)** authenticate fully even for one user; **(B)** *sensitive capabilities* (shell, Python, fs-write, email-send, vault, config) are gated by **explicit user approval** rather than a vacuous admin-vs-user check — a sensitive tool call **parks the Run and asks** before it executes, which is what actually constrains the autonomous agent; **(C)** multi-tenancy is **seam-only** — every record carries an `owner_id` defaulting to the operator, but isolation enforcement and per-user management are deferred until a second human exists. Reusable secrets encrypted at rest, auth secrets one-way hashed (`XC-SEC-3`). Untrusted external content is wrapped as data, not instructions, before it enters a prompt (`XC-SEC-5`). All user data is encrypted at rest (AES-256, password-derived key, lock-until-unlocked). Agent-invoked **commands and code run in a per-conversation VM** that mounts only the folders of the agent's home, with network limited to an egress allowlist and fail-closed when no VM is available (file tools keep working on the host folders); direct host execution is reachable only through one distinct, explained, approval-gated tool inside a confining fence (`XC-SEC-7`).
- **Persistence.** SQLite via an async ORM, schema applied/upgraded automatically on startup (`XC-DATA-2`) — no manual migration step. Conversation history uses a **write-behind** model: Pydantic AI's in-memory `ModelMessage` history is the live working set during a turn; completed messages are copied onto a persistence queue that drains to the DB off the hot path, stored in serializable `ModelMessage` form for resume fidelity plus a derived projection for listing/search.
- **Config.** Deploy-level secrets/defaults from `.env` before first boot (`XC-CFG-1`); everything else is runtime-mutable settings persisted to the DB (`XC-CFG-2`).
- **Degradation is designed in, not bolted on.** Vector search, web search, mail, push, and model endpoints are all optional capabilities; their absence degrades the dependent feature to a clear, bounded fallback rather than an error or a hang (`XC-DEG-*`, `DR-4.1`).

→ detail: [`00-principles-and-layout.md`](./00-principles-and-layout.md), data model in [`60-data-model.md`](./60-data-model.md)

---

## 4. Proposed repository layout

Backend lives under `backend/`, parallel to `frontend/`.

```
backend/
  app.py            # FastAPI assembly: middleware, auth, router registration, run registry on app state
  harness/          # app-assembly substrate: the lifecycle registry, feature manifests, discovery,
                    #   and the built-in plugin switches
  plugins/          # built-in plugins (mail, routines, discord), each a manifest with plugin metadata
  pyproject.toml    # uv-managed
  core/             # foundation: config, db engine + schema/migrations, auth, security/crypto, exceptions
  models/           # ORM entities + Pydantic schemas (the data contracts)
  runs/             # the Run substrate: registry, broker, event buffer, event protocol, transports (Pillar I+II)
  agent/            # the engine: Agent assembly, RunDeps/RunContext, the meta-loop (verifier, loop-break),
                    #             history processors, event translation
  tools/            # tool definitions grouped by AE-2 category — thin adapters over services/
  services/         # capabilities: llm, embeddings, vectorstore, search, memory, registry, mail, dav, notify
  routes/           # thin FastAPI routers, one per feature surface
  tests/
```

Dependency direction is strictly downward: `routes → agent → tools → services → core`. `runs/` is foundation the orchestrators sit on. Nothing in `services/` imports an orchestrator.

---

## 5. How a chat turn flows (the whole thing, end to end)

1. `POST /runs` (or `/chat`) authenticates, resolves the user's privileges, creates a **Run**, registers its asyncio task, and returns a run id immediately.
2. The client opens `GET /runs/{id}/events` (SSE) and starts receiving the event stream; `Last-Event-ID` makes reconnects replay-correct.
3. The orchestrator assembles `RunDeps` (user, privileges, enabled-tool set, the event emitter, capability handles, any open document) and builds the **toolset stack** for this user (privilege gate → enable gate → naming; no relevance pre-filter).
4. It runs the Pydantic AI `Agent` via `agent.iter()`, translating each node's stream into domain events (reasoning, text deltas, tool start/progress/result, step boundaries) and publishing them through the broker to the SSE transport — while also persisting durable artifacts (messages, documents, metrics).
5. Tools receive `RunContext.deps` and can **emit their own progress events** (so a slow tool shows elapsed time and partial output, `AE-6.1`) and reach capabilities without globals.
6. On the model's final output, the **meta-loop** optionally verifies deliverables and may make one bounded corrective re-attempt; then the orchestrator emits final metrics and an explicit end-of-turn, and persists the updated `ModelMessage` history.
7. Throughout, the inactivity watchdog — and a wall-clock limit, where one is configured — can cut the run off; a `POST /runs/{id}/cancel` stops it at the next step boundary. Disconnect at any point leaves the run running and resumable.

Deep research is not a different skeleton. It is the `research` platform skill that any chat opens: clarify the question, submit a plan of topics for the operator to approve, then launch a `researcher` agent per topic (`agents_spawn`). Each researcher is an ordinary turn in a hidden conversation that reports back and wakes the thread that launched it. Concurrency is the lane's; when to stop is the model's own call, checked against `agents_list`.

---

## 6. Build status

Document streaming with auto-promotion (`AE-6.2/6.4`, `DOC-3`) and attachment/upload ingestion (`CHAT-2`, `UP-*`) are deferred until their features are in scope; the foundation does not depend on them.

The foundation is built: Pillars I–III, approval, memory, auth, at-rest encryption, the model registry and the per-conversation VM for commands and code live under `backend/`. What is built is what the tests hold. Detail docs written so far: [`40-tools-and-toolsets.md`](./40-tools-and-toolsets.md) (the gating stack, VM isolation, scheduled-task pre-authorization, external-tool gating) and [`45-plugins.md`](./45-plugins.md) (built-in and sandboxed plugins, contribution types, plugin windows and their bridge, messaging channels). Remaining detail docs (`10-run-substrate.md`, `20-event-protocol.md`, `30-agent-engine.md`, `50-capabilities.md`, `60-data-model.md`) are still to be written.
