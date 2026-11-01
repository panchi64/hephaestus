# Hephaestus

```
HEPH-HUD ── AGENT PLATFORM ─────────────────────────────────── ■ ALL SYSTEMS
  HOST      self-hosted · single operator · your hardware
  DATA      AES-256 at rest · key derived from your password, held in memory
  AGENTS    define · delegate · message · open any sub-agent as a thread
  EXTEND    plugins · skills · routines · workflows
  CONTEXT   searched on demand · never injected
  MODELS    any OpenAI-compatible, Anthropic or Google endpoint
────────────────────────────────────────────────────────────────────────────
```

A self-hosted agent platform: you hand it work, agents plan it, split it between them, carry it out, and write the tools they are missing, and nothing they build goes live until you approve it.

![The Hephaestus home page: the launch composer, recent threads, two runs in flight, and per-capability status along the bottom.](md-assets/hephaestus-screenshot.png)

<sub>Dev instance with a scripted model. The amber cells are real: it runs without the platform VM.</sub>

## What it does

**Agents**

- **One chat, every capability.** No modes; every message runs the full agent loop over the tools your permission level allows.
- **Agents you define.** A brief, a model, a tool list and a ceiling. Write them yourself, add them to a project's `.claude/agents/`, or approve one an agent drafted.
- **Delegation trees.** Agents spawn agents to any depth you allow, work in parallel, and message each other. Reports arrive on their own; nothing blocks.
- **Every sub-agent is a thread** you can open, read and talk to.
- **Per-endpoint queueing.** Each model endpoint has a capacity; runs past it wait in line, your own messages first.

**Extending it**

- **Plugins.** Agents write Python tools, test them in the platform VM, and hand them to you to publish. Secrets never reach plugin code.
- **Skills.** Know-how loaded on demand: `research`, `delegation`, `build-an-agent` and `build-a-plugin` ship built in, alongside a project's `.claude/skills/` and your own.
- **Routines.** Scheduled or webhook-triggered runs by the agent you choose, always on time, each in its own thread.
- **Workflows.** Slash commands you write once.

**Context and work**

- **`platform_search`** reaches other conversations, memory, your files, reports and routine outputs on demand, so a chat's context holds only that chat.
- **Memory, Knowledge Base, compaction.** Durable facts, retrieval over your folders, and long threads folded into summaries.
- **Projects.** A chat works in your folder under your permission level, or on its own git worktree that merges back only when you say.
- **The platform VM** runs the sandbox, project shell, browser and SearXNG. Nothing is mounted into it, and its only network exit is an allowlist.

**Reach and controls**

- Web search and fetch, email (IMAP/SMTP, Gmail, Graph, JMAP), calendar with CalDAV, MCP servers and integrations, and notifications by email and push.
- Permission levels from plan-only to fully autonomous, a model registry with fallback chains, a vault, API tokens, encrypted backup, and offline mode.

## How it's built

Pydantic AI runs the agent loop; the code around it makes each run durable, resumable and governed. Every chat turn, sub-agent turn and routine run is a **Run**, and all logic lives in the backend.

```
frontend/   SolidStart · TypeScript · Tailwind v4
backend/    Pydantic AI + FastAPI (Python 3.14, uv)
  agent/ runs/ tools/ services/ harness/ skills/ vm/
docs/       architecture, design system
```

Design: [`docs/architecture/`](docs/architecture/README.md).

## Running it

```bash
cd backend && uv sync && uv run python dev.py      # API on :8000
cd frontend && bun install && bun run dev          # UI on :5173, in a second terminal
```

Create the operator account in the browser on first launch; its password derives the encryption key. Point Hephaestus at model endpoints you already run (LM Studio, vLLM, llama.cpp) or at a hosted provider.

The platform VM needs [QEMU](https://www.qemu.org), fast with KVM or Hypervisor.framework. Its first boot builds the guest image, which takes a few minutes. Without it, the sandbox, shell and web tools report unavailable; nothing falls back to the host.

Checks: `uv run pytest` and `uv run ruff check .` in `backend/`; `bun run typecheck`, `lint` and `test` in `frontend/`. A disposable, seeded instance with a scripted model: [`docs/dev-instance.md`](docs/dev-instance.md).

## Security

Agent workspaces are disposable; anything that leaves them (your host, your real files, email, the vault, a new domain) asks first. Everything agents build is a draft until you publish it. All data is encrypted at rest under a key held only in memory, and untrusted content reaches the model as data, never as instructions. Details and deployment advice: [SECURITY.md](SECURITY.md).

## What's left

- Routine chaining, and event triggers beyond the inbound webhook.
- The health screen, which still renders fixtures.

## Contributing

Help is welcome. The architecture docs are the source of truth; the rationale for a trade-off, including the alternatives rejected, goes in the commit message. Requirement ids in docstrings (`XC-*`, `AE-*`) are historical; where one disagrees with the code, the code is right.

## Lineage and license

Hephaestus began as **Odysseus**, a rebuild of PewDiePie's original self-hosted workspace, and outgrew the name. MIT licensed; see [LICENSE](LICENSE) and [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md).

```
SYSTEM  HEPHAESTUS · MIT · SELF-HOSTED ─────────────────────────────── ■ READY
```
