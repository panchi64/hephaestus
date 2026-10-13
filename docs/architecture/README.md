# Odysseus — Backend Architecture

> **Status: living design.** This describes *how* the backend is built — the structure, abstractions, and seams.
>
> **The black-box spec that seeded this design has been retired.** It did its job keeping a greenfield rebuild on track and the build has outgrown it; the requirement ids still cited in places below and in the code are historical, and where one disagrees with the code, the code is right. What remains authoritative is this document, [`decisions.md`](./decisions.md), and the capabilities of **FastAPI** + **Pydantic AI**.
>
> **Greenfield.** Nothing here references any prior implementation. It is derived from the deployment profile (single operator, one powerful local host, models with native tool-calling) and the two libraries above.
>
> Decisions that have real trade-offs are **not settled here** — they live in [`decisions.md`](./decisions.md) with their angles, and are flagged inline as **⟦OPEN: Dn⟧**.

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

**Pydantic AI is the engine; we are the chassis.** We never re-implement what Pydantic AI does well (the loop, validation, fallback, streaming primitives). We *do* own everything that turns one model run into a durable, observable, multi-user, resumable product feature — because none of that is the library's job.

This is the answer to "how tightly do we couple to Pydantic AI": **all agentic logic goes through it; all orchestration, transport, and state around it is ours.**

---

## 2. The three pillars

Everything is built on three layers. Read them in this order.

### Pillar I — The Run substrate (the chassis)

A **Run** is the central abstraction: one server-side, identified, background-executing unit of work for one user request. Chat turns, sub-agent turns and routine runs are all Runs — they differ only in which *orchestrator* drives them, not in how they are launched, streamed, observed, resumed, or cancelled.

A Run owns:

- an **id, owner, and status** state machine (`queued → running → {done | blocked | error | cancelled}`, with a parked `awaiting_input` state when a sensitive tool call needs approval — D20),
- a **typed, sequence-numbered event stream** (Pillar II) published to an in-process broker,
- an **in-memory event buffer** so a client that disconnects and reconnects can replay what it missed (`AE-7`) — this lives only as long as the server process, which is exactly what the spec requires,
- a background **asyncio task** tracked in a `RunRegistry`, decoupled from any client connection: closing the browser does not stop the work (`AE-7.1`),
- **bounds**: a max-step ceiling (`AE-1.5`), optional tool-call ceiling (`AE-1.6`), an inactivity watchdog and an opt-in wall-clock limit (`XC-PERF-2`), and cancellation that takes effect at the next step boundary (`DR-3.3`, `CHAT-5`). The step ceiling and the watchdog apply to every run; the wall clock is off unless the deploy or the operator sets one, since a turn that is merely slow is a turn we want to let finish.
- **a lane**: concurrency is bounded per lane rather than by one shared pool — `interactive` (the operator's own turn), `background` (a scheduled task), `linked` (a thread the agent opened for itself) — picked from the run's `kind`, so unattended work can never leave a turn somebody is waiting on queued at the gate,
- **an endpoint lease**: past its lane, a run waits in line for its model endpoint's capacity and holds it for the whole turn (`runs/endpoint_gate.py`, D117), announcing its place with `run.queued`.

Because every long-running feature is a Run, continuity/resume/cancel/timeout/metrics are written **once** and inherited everywhere. ⟦OPEN: D1 transport, D2 concurrency model⟧

→ detail: [`10-run-substrate.md`](./10-run-substrate.md)

### Pillar II — The event protocol (the contract)

`AE-6` enumerates what must stream to the client: incremental answer text; reasoning kept *separate* from the answer (`AE-6.3`); each tool's start/progress/result; live progress for slow tools (elapsed + partial output); step boundaries; document create/stream/commit (`AE-6.2`); cited web sources; budget-limit notices; final run metrics; errors; and an explicit end-of-turn. We add one event the spec implies via gating: an **`approval_required`** event (the pending sensitive action + its validated args), paired with the `awaiting_input` run state and a `POST …/approve` control (D20).

We model this as **one versioned, typed union of events** (Pydantic models), each carrying a monotonic `seq` for `Last-Event-ID` resume. This union *is* the backend↔frontend contract — it replaces any ad-hoc shapes. **The v1 set is frozen (D15):** `run.*`, `step.*`, `thinking.delta`/`answer.delta`, `tool.*`, `document.*`, and the `citation.added`/`approval.required`/`limit.notice` notices — named `entity.event`, dot.lowercase, past-tense for discrete events and `delta`/`progress` for streams. The agent engine produces it by **translating Pydantic AI's native stream** (`PartStartEvent`/`PartDeltaEvent` for text and thinking parts, tool-call and tool-return events, `FinalResultEvent`) into our domain events, and by emitting the events Pydantic AI knows nothing about (step boundaries, document lifecycle, budget notices, run metrics, end-of-turn).

→ detail: [`20-event-protocol.md`](./20-event-protocol.md)

### Pillar III — The agent engine, tools, and capabilities (the engine + its reach)

Three nested concerns:

1. **The agent engine** wraps Pydantic AI's `Agent`. Within a turn we let the `Agent` run its multi-step loop (driven via `agent.iter()` so we can observe each graph node and stream it). *Around* that we own the **meta-loop**: the post-turn verifier that checks each promised deliverable actually exists (`AE-1.4`, `AE-5.2`) and, if not, makes a *bounded* corrective re-attempt (`AE-5.5`); and the loop-breaker that aborts on no-progress repetition (`AE-5.1`). Context reduction near the model's limit (`AE-5.4`, `CHAT-4`) is a Pydantic AI history processor. Model fallback (`AE-5.3`) is `FallbackModel`. ⟦OPEN: D4 verifier policy, D5 chat-vs-agent routing, D6 context reduction⟧

2. **Tools** are exposed to the model as Pydantic AI toolsets, but *which* tools a given run sees is **our policy**, expressed as a stack of toolset wrappers evaluated against the run's `RunContext` deps:

   ```
   CombinedToolset(all categories + MCP servers + integrations)
     → .filtered(privilege_gate)     # AE-3.1 / AE-3.2  drop tools above the user's tier
     → .filtered(enabled_gate)       # AE-3.3           drop user-disabled tools
     → .renamed(namespacing)         #                  stable "category.tool" names
     # (no relevance pre-filter — see D3: the model discerns from the full gated catalog;
     #  a .prepared() relevance step can slot in here later if the catalog ever grows too large)
   ```

   This is the single most leveraged mapping in the design: the spec's entire access-control + namespacing story is *composition of library primitives keyed on per-run dependencies*, not bespoke machinery. Per **D3** we deliberately omit relevance pre-filtering (`AE-4.1`, a waivable SHOULD-performance) — capable native-tool-call models on one powerful host select their own tools, and `AE-4.2` is trivially met since every tool is always present.

3. **Capabilities** (`services/`) are the actual implementations — web search, vector store, memory, embeddings, the model registry, mail — each an async interface with a graceful-degradation story (`XC-DEG-*`). **Tools are thin adapters over capabilities.** The same capability is reused by a tool (agent calls it) and by a plain REST route (user calls it directly). Logic never hides inside a tool.

→ details: [`30-agent-engine.md`](./30-agent-engine.md), [`40-tools-and-toolsets.md`](./40-tools-and-toolsets.md), [`50-capabilities.md`](./50-capabilities.md)

---

## 3. Cross-cutting foundations

- **Platform-agnostic by construction.** The backend MUST run on Linux, macOS, and other POSIX hosts with no OS-specific dependency. Concretely: **no OS keystore** (the encryption key is password-derived and memory-only — D17), filesystem access via `pathlib` not hard-coded separators, no shelling out to platform-only binaries in the core, and all crypto/storage deps chosen for cross-platform wheels (SQLCipher, `argon2-cffi`, pyca `cryptography`, sqlite-vec). Model inference is an HTTP call to a local OpenAI-compatible server, which is itself OS-portable. Any genuinely platform-specific capability (if one ever appears) must sit behind an interface with a portable default.
- **Topology — origin-agnostic.** The backend is a pure API: **bearer-token** auth (works same-origin or split-origin), CORS configurable, no assumption about who serves the SPA. A deployment may put the API and the built frontend behind one reverse proxy or run them apart; the backend does not care. ⟦OPEN: D9⟧
- **Security posture (D14, single-operator).** Auth is enforced **globally** by middleware before any feature is reached (`XC-SEC-1`). The spec's "privilege" is split into three axes: **(A)** authenticate fully even for one user; **(B)** *sensitive capabilities* (shell, Python, fs-write, email-send, vault, config) are gated by **explicit user approval** rather than a vacuous admin-vs-user check — a sensitive tool call **parks the Run and asks** before it executes (D20), which is what actually constrains the autonomous agent; **(C)** multi-tenancy is **seam-only** — every record carries an `owner_id` defaulting to the operator, but isolation enforcement and per-user management are deferred until a second human exists. Reusable secrets encrypted at rest, auth secrets one-way hashed (`XC-SEC-3`). Untrusted external content is wrapped as data, not instructions, before it enters a prompt (`XC-SEC-5`). All user data is encrypted at rest (AES-256, password-derived key, lock-until-unlocked — D17). Agent-invoked **code execution is isolated from the host** — sandboxed on copies, fail-closed when no sandbox runtime is available, with direct host execution reachable only through a distinct, explained, approval-gated tool (D23, `XC-SEC-7`). ⟦OPEN: D11 untrusted-content technique⟧
- **Persistence.** SQLite via an async ORM, schema applied/upgraded automatically on startup (`XC-DATA-2`) — no manual migration step. Conversation history uses a **write-behind** model (D8): Pydantic AI's in-memory `ModelMessage` history is the live working set during a turn; completed messages are copied onto a persistence queue that drains to the DB off the hot path, stored in serializable `ModelMessage` form for resume fidelity plus a derived projection for listing/search. ⟦OPEN: D7 migration strategy, D12 ORM⟧
- **Config.** Deploy-level secrets/defaults from `.env` before first boot (`XC-CFG-1`); everything else is runtime-mutable settings persisted to the DB (`XC-CFG-2`).
- **Degradation is designed in, not bolted on.** Vector search, web search, mail, push, and model endpoints are all optional capabilities; their absence degrades the dependent feature to a clear, bounded fallback rather than an error or a hang (`XC-DEG-*`, `DR-4.1`).

→ detail: [`00-principles-and-layout.md`](./00-principles-and-layout.md), data model in [`60-data-model.md`](./60-data-model.md)

---

## 4. Proposed repository layout

Backend lives under `backend/`, parallel to `frontend/`. ⟦OPEN: D10 package granularity⟧

```
backend/
  app.py            # FastAPI assembly: middleware, auth, router registration, run registry on app state
  harness/          # app-assembly substrate: the lifecycle registry (start/stop order for all background
                    #   work — D33); feature manifests + discovery land here as they arrive
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
3. The orchestrator assembles `RunDeps` (user, privileges, enabled-tool set, the event emitter, capability handles, any open document) and builds the **toolset stack** for this user (privilege gate → enable gate → naming; no relevance pre-filter, per D3).
4. It runs the Pydantic AI `Agent` via `agent.iter()`, translating each node's stream into domain events (reasoning, text deltas, tool start/progress/result, step boundaries) and publishing them through the broker to the SSE transport — while also persisting durable artifacts (messages, documents, metrics).
5. Tools receive `RunContext.deps` and can **emit their own progress events** (so a slow tool shows elapsed time and partial output, `AE-6.1`) and reach capabilities without globals.
6. On the model's final output, the **meta-loop** optionally verifies deliverables and may make one bounded corrective re-attempt; then the orchestrator emits final metrics and an explicit end-of-turn, and persists the updated `ModelMessage` history.
7. Throughout, the inactivity watchdog — and a wall-clock limit, where one is configured — can cut the run off; a `POST /runs/{id}/cancel` stops it at the next step boundary. Disconnect at any point leaves the run running and resumable.

Deep research is **not** a different skeleton. It was once a rounds-based pipeline of its own, then a mode a conversation could be in, and is now the `research` platform skill (D124) that any chat opens: clarify the question, submit a plan of topics for the operator to approve, then launch a `researcher` agent per topic (`agents_spawn`). Each researcher is an ordinary turn in a hidden conversation that reports back and wakes the thread that launched it. The rounds loop's concurrency is now the lane's, and its "stop when comprehensive" is the model's own, checked against `agents_list`.

---

## 6. Decision status

All decisions with real trade-offs are tracked in **[`decisions.md`](./decisions.md)**. As of this pass, **every foundational decision (D1–D20) is settled.**

**Decided:**

- **D1** SSE + POST control · **D2** in-process, single-process, asyncio + thread/process pools · **D3** no relevance layer (model discerns) · **D4** verifier: heuristic, configurable, capped · **D5** single always-agent path
- **D6** context reduction: history-processor hybrid (impl deferred) · **D7** Alembic from day one · **D8** write-behind history · **D9** dual httpOnly-cookie + bearer · **D10** fine package split
- **D11** `wrap_untrusted()` + `ReinjectSystemPrompt` (technique deferred) · **D12** SQLModel, sync DB in threadpool · **D13** in-process lock-aware scheduler · **D14** single-operator (auth + capability-approval + ownership-seam)
- **D15** v1 event protocol frozen · **D16** named model roles + single utility · **D17** all-data AES-256 at rest, password-derived lock-until-unlocked · **D18** pluggable vector store, sqlite-vec default · **D19** research: hybrid pipeline+agent (superseded by D124) · **D20** approval-gated sensitive tools · **D23** code-execution isolation (sandboxed by default, host only via explained approval) · **D24** pre-authorized scheduled tasks (approval at scheduling time, scoped standing grant) · **D25** external-tool gating (MCP/integrations sensitive by default, trust opt-out)
- The agent platform: **D114** no modes; a chat's workspace follows its project · **D115** one agent definition · **D116** delegation trees with messaging · **D117** per-endpoint run queueing · **D118** the platform is searched, not injected · **D119** one platform VM · **D121** version history · **D122** plugins · **D123** routines run on time · **D124** platform skills and a lean system prompt

**Deferred to when their feature is in scope** (not blocking the foundation): **D21** document streaming & auto-promotion (`AE-6.2/6.4`, `DOC-3`) · **D22** attachment/upload ingestion (`CHAT-2`, `UP-*`).

The foundation is built — Pillars I–III, approval, memory, auth, at-rest encryption, the model registry, and the code-execution sandbox live under `backend/`. Detail docs written so far: [`40-tools-and-toolsets.md`](./40-tools-and-toolsets.md) (the gating stack + D20/D23/D24/D25). The spec-coverage matrix is gone with the spec — what is built is what the tests hold. Remaining detail docs (`10-run-substrate.md`, `20-event-protocol.md`, `30-agent-engine.md`, `50-capabilities.md`, `60-data-model.md`) are still to be written.
