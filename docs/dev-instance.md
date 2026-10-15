# The dev instance

A second, disposable Hephaestus you can run, drive and break: its own data directory, its
own ports, its own platform VM, and a scripted model behind it. Nothing it does touches
the operator's workspace.

It exists because the test suite proves behaviour and cannot show a change working. A
picker, a card, a layout, a flow — those are either seen in the running product or taken
on trust, and the only other running instance holds someone's real conversations and
credentials.

> **This file is the source of truth for the agent-facing copies.** `.claude/` is
> gitignored on purpose, so it doesn't travel to a new clone or a new git worktree.
> `dev_instance.py up` materializes the launch config and the project skill from this file
> on every run (`backend/devkit/surfaces.py`). Edit the recipe here, not in the generated
> copies.

## Running the app to see a change

To see a change working in the real product — or to screenshot a surface, or to check a
flow end to end — bring up the isolated dev instance:

```bash
cd backend && uv run python dev_instance.py up --detach
```

It is safe to run twice; it fills in whatever is not already running, seeds the workspace
on first use, and prints the URLs. Then `preview_start 'hephaestus-dev'` opens it in the
browser pane. Ports differ per worktree — read them from
`uv run python dev_instance.py status --json`, never assume them.

This instance is **not** the operator's: separate data directory, separate ports,
a separate VM, a stub model. Nothing in it is real, and breaking it costs nothing.

## Getting it running

```bash
cd backend
uv run python dev_instance.py up --detach     # start everything, seed on first use
uv run python dev_instance.py status --json   # ports, URLs, what is running
uv run python dev_instance.py stop            # stop what `up` started
```

`up` **converges**: it looks at what is already listening and starts only the gaps, so
running it again is safe and cheap. Without `--detach` it stays in the foreground and
Ctrl-C takes the services down with it.

On a first run in a fresh worktree it also installs the frontend's dependencies and warms
vite's dependency optimizer, so the first page you open is the app and not a blank screen.

## Looking at it

```
preview_start 'hephaestus-dev'      # opens the browser pane at this worktree's frontend
```

Then `read_page`, `find`, `computer` (click, type, screenshot) as usual, and
`read_console_messages` / `read_network_requests` when something does not render — a CORS
or 401 problem shows up there immediately and nowhere else.

There is **no login screen** by default: auth is off and the vault is unlocked at boot, so
navigating lands you in the app. Use `up --auth` when the login and setup screens are
themselves what you are working on; the password is in `status --json`.

## What is in it

Seeded on first use, and re-seeded automatically whenever the fixture pack or the database
schema changes:

- a model endpoint bound to every role, pointed at the stub
- two conversations, one of which called a tool
- a calendar with three upcoming events
- three memories
- one scheduled task, disabled

Data persists between sessions, so a scenario you build up in one is still there in the
next. `reset --yes` wipes the workspace; the next `up` re-seeds it.

## The model

By default a scripted stub on its own port — deterministic, free, offline, and able to do
things a real endpoint will not: fail on demand, take four seconds, call a tool you name.

```bash
# make it say something, call a tool, stall, or fail
curl -X POST http://127.0.0.1:{{stub_port}}/_stub/script -H 'content-type: application/json' -d '[
  {"match": "weather", "text": "It is raining."},
  {"match": "add", "tool_calls": [{"name": "calculator", "arguments": "{\"a\": 2}"}]},
  {"match": "slow", "text": "eventually", "latency_s": 4},
  {"match": "boom", "status": 500, "error": "upstream exploded"}
]'

curl http://127.0.0.1:{{stub_port}}/_stub/requests   # what the agent actually sent
curl -X POST http://127.0.0.1:{{stub_port}}/_stub/reset
```

Two rules worth knowing before you script one:

- Scenarios match on the **last user message**, not on a call index — an agent turn is a
  loop, so a script keyed by call count says different things depending on how many tools
  the agent chose to call.
- A scenario with `tool_calls` is only given to a request that **offered** tools. One user
  message produces several requests, and the auto-titler beside the turn carries the same
  last user message. Set `"needs_tools": false` for a reply aimed at the titler.

To use a real model instead, name one before `up` — the stub is then not started at all:

```bash
export HEPH_DEV_CHAT_BASE_URL=http://127.0.0.1:1234/v1
export HEPH_DEV_CHAT_MODEL=your-model
export HEPH_DEV_CHAT_KEY=…          # optional; a local engine usually needs none
export HEPH_DEV_EMBED_MODEL=…       # optional; an embedding model on the same server
```

An instance already seeded against the stub keeps its bindings until `up --reseed`.

## Measuring task success

`evals/tasks/` runs real tasks through a live dev instance and records how each one ended:

```bash
uv run python dev_instance.py up --detach --with-vm --reseed   # with HEPH_DEV_CHAT_* set
uv run python -m evals.tasks.runner                  # or --only build-primes admin-routine
```

Each task is a new thread at the default permission level. Parked approvals are answered
by the task's policy, and questions are answered with "use your best judgement". Results
go to `evals/tasks/results/<label>.jsonl`, with a markdown summary of outcomes and causes
beside it. Tasks that need the VM are skipped without `--with-vm`.

## The platform VM, and why it is off

The platform VM — and with it the sandbox, the project shell, SearXNG and the web-fetch
browser — does not run by default: it costs an image build, memory and boot time that most
visual checks never need. `up --with-vm` turns it on. The VM and its image live under this
instance's own data dir; to skip the first image build, point `HEPHAESTUS_VM_IMAGE` at an
image directory another instance already built (`<data dir>/vm/images/<hash>`).

## When something is wrong

```bash
uv run python dev_instance.py status --json
tail -f ~/.hephaestus/dev/<worktree>/logs/backend.log    # also stub.log, frontend.log
```

Everything lives under `~/.hephaestus/dev/<worktree>/` — data, worktrees, logs, and the
`instance.json` recording the ports and the vault passphrase. Deleting that directory
forgets the instance entirely; the next `up` builds a new one.

## Running `alembic` by hand

The CLI falls back to `alembic.ini`, which points at the live `data/app.db`. A command meant
for a dev instance must carry that instance's `HEPHAESTUS_DB_URL` (`status --json` has the
data directory), or it migrates the real database.

## What it is not

Not a place to point at real data. `HEPHAESTUS_DATA_DIR` and the rest are set by the
launcher for exactly this reason: an instance aimed at the operator's data directory would
seed fixtures into their workspace and boot a second VM over their state.
