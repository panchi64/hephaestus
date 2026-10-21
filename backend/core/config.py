"""Application configuration.

Deploy-level secrets and defaults come from the environment / ``.env`` before
first boot. Runtime-mutable user settings live in the DB and are
not modeled here.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_prefix="HEPHAESTUS_", env_file=".env", extra="ignore"
    )

    version: str = "0.1.0"
    environment: str = "development"

    host: str = "127.0.0.1"
    # 8000, not 7000: macOS AirPlay Receiver squats on :7000 (wildcard, both IP
    # stacks), shadowing an IPv4-only bind when localhost resolves to ::1.
    port: int = 8000

    # Origin-agnostic: the frontend origins allowed to call the API. Both localhost
    # and 127.0.0.1 are listed since browsers treat them as distinct origins.
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    auth_enabled: bool = True

    # All user data lives under here — gitignored, encrypted at rest.
    data_dir: Path = Path("data")

    @field_validator("data_dir")
    @classmethod
    def _absolute_data_dir(cls, value: Path) -> Path:
        # Anchor to an absolute host path once, here, so every consumer of a path beneath
        # this resolves the same directory whatever the working directory is.
        return value.expanduser().resolve()

    # Where the chats' git worktrees are checked out. **Deliberately outside
    # `data_dir`**: an approved host command is fenced by `services/sandbox/fence.py`,
    # which denies reads of the whole data directory (the vault, the sealed workspaces
    # and the DB live there). A worktree under `data_dir` would therefore be unreadable
    # by the very shell that has to build and test it. The tradeoff this accepts is that
    # a worktree is plaintext on disk — it is a checkout of the operator's own already
    # plaintext repository, so it exposes nothing that was not already exposed.
    worktrees_dir: Path = Path("~/.hephaestus/worktrees")

    @field_validator("worktrees_dir")
    @classmethod
    def _absolute_worktrees_dir(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    # DB connection. None ⇒ a file under data_dir; tests pass an in-memory URL.
    db_url: str | None = None
    # Unlock passphrase for the at-rest encryption vault. When set, the vault is
    # set up (first run) or unlocked at boot without a login — the auth-disabled
    # path. With auth enabled the operator unlocks via login instead.
    unlock_passphrase: str | None = None

    # Run substrate bounds. Timeouts are seconds; None disables.
    #
    # Concurrency is carved into **lanes** (`runs/lanes.py`) rather than shared blindly:
    # the operator's own turn must not queue behind a scheduled task or behind threads the
    # agent opened for itself. `run_max_concurrency` keeps its name, its number and its
    # meaning — the host ceiling, across every lane; the two unattended lanes are capped
    # well below it on purpose, because nobody is waiting on them and their failure mode
    # is volume, and slots they can never hold are slots the operator always can.
    run_max_concurrency: int = 8
    run_background_concurrency: int = 2
    # Routine runs, reserved on top of `run_max_concurrency`: a routine runs on schedule, so
    # it neither waits for the operator's slots nor takes them.
    run_routine_concurrency: int = 2
    # Also the number of sub-agents that actually *execute* at once, since they run in the
    # linked lane. Deliberately left as one knob rather than
    # widened for them: the lane's narrowness is what guarantees the operator's own turn a
    # floor of slots, so a burst of sub-agents queues instead of crowding the person out.
    # The operator's `chat.subagent_max_concurrent` setting is a different bound and does
    # not feed this one — that caps how many may *exist*, which is about the operator's
    # attention and their bill; this caps how many may run, which is about the host.
    run_linked_concurrency: int = 3
    # Off by default: a wall clock mostly fires on a run that is legitimately slow — a local
    # model, a long sandboxed tool call — which is the run we want to let finish. It is an
    # opt-in lever for the gap the inactivity watchdog leaves: that reads activity off the
    # event stream, so a run that keeps *emitting* refreshes it on every frame.
    run_wall_clock_timeout_s: float | None = None
    run_inactivity_timeout_s: float | None = 120.0
    # How long a model request may stream nothing before its first part. The inactivity
    # clock is held open meanwhile, because reading a long prompt is silent: about 2s per
    # 1k new tokens for a 27B model on Apple silicon, so a cold 50k-token thread waits
    # around 100s. None leaves that wait to the inactivity bound.
    first_token_timeout_s: float | None = 600.0
    # One tool call's bound. A call holds the inactivity clock open while it runs (a VM
    # bring-up or a build streams nothing), so this is what stops one that wedges.
    tool_call_timeout_s: float | None = 1800.0
    # How long shutdown spends on any one drain — flushing a write-behind queue, waiting
    # for cancelled runs to unwind, closing a browser — before moving on. A stop that
    # waits forever on a stuck job is a Ctrl+C that has to be repeated, and the second one
    # skips the rest of the shutdown; a bounded wait loses at most the one job that was
    # already not finishing.
    shutdown_drain_timeout_s: float = 5.0

    # Model resolution is the DB-backed registry's job (services/registry.py) —
    # named roles bound to ordered endpoint chains, the single source of truth,
    # populated by manual config (the /models surface) today and the automatic
    # setup later. There is deliberately no env model seam.

    # A turn has no request or tool-call budget; the loop guard and the timeouts bound it.
    # How many times a turn re-sends a request a transient model error cut short (a
    # dropped connection, a 5xx or 429, a stream that ended mid-call).
    auto_continue_transient_max: int = 2
    # How many times the model may retry a tool call whose arguments a tool rejected, and
    # an answer that failed output validation, before the turn fails.
    agent_tool_retries: int = 3
    agent_output_retries: int = 2

    # `run_code` — the in-process Monty sandbox a model-written script calls tools from
    # (`agent/code_mode.py`). The duration and memory caps are per snippet. The call cap is
    # how many tools one script may call.
    code_mode_max_duration_s: float = 30.0
    code_mode_max_memory_mb: int = 256
    code_mode_max_tool_calls: int = 25

    # How long a sub-agent's turn may run before the substrate ends it. The one place a wall
    # clock is on by default, because nobody is sitting in front of it: the inactivity
    # watchdog cannot end it (a model streaming tokens refreshes that clock on every
    # frame), the request limit only bounds round trips, and the linked lane is
    # `run_linked_concurrency` wide. Eighteen minutes: a thorough read of a dozen sources
    # fits comfortably, a stuck one does not.
    subagent_wall_clock_timeout_s: float | None = 1080.0

    # How deep a delegation tree may grow: a thread's own agents are depth 1. An agent's
    # definition may lower it for the agents below it, never raise it.
    agent_max_depth: int = 3

    # The platform VM: one microVM (smolvm, on QEMU) per installation, where agent
    # commands, previews, headless Chromium and SearXNG run. It boots in the background
    # from a clean image built once from `backend/vm/provision.sh` (or `vm_image`, a
    # directory holding a prebuilt one), and nothing durable lives in it. Without it the
    # capabilities that need it report unavailable (fail closed — never a host fallback).
    # `vm_ca_bundle` is a CA bundle the guest trusts as well, for networks that inspect
    # TLS. `vm_transfer_max_bytes` caps what one move of a workspace out of the VM may
    # carry.
    vm_enabled: bool = True
    vm_cpus: int = 4
    vm_memory_mb: int = 6144
    vm_disk_gb: int = 32
    vm_image: str | None = None
    vm_ca_bundle: str | None = None
    vm_boot_timeout_s: float = 300.0
    vm_provision_timeout_s: float = 3600.0
    vm_transfer_max_bytes: int = 4 * 1024**3

    # Execution sandbox: each conversation's workspace, run in the platform VM.
    sandbox_enabled: bool = True
    # What one workspace's commands may consume between them, so one runaway workspace
    # cannot starve the others sharing the VM — not to keep the agent small. Set
    # generously enough that ordinary work (a build, a dataframe, a test suite) never
    # notices them, so a failure that does hit one is genuinely
    # pathological and worth reporting as such.
    sandbox_memory: str = "4g"
    sandbox_cpus: str = "2.0"
    # Max processes/threads a single execution may spawn — a crude fork-bomb guard.
    # Reported to the model alongside memory/cpus (`tools/code.py`) so a fork/thread
    # failure can be attributed to this cap instead of only "possibly OOM".
    sandbox_pids_limit: int = 1024
    # How much of a file one `files_read_file` call returns. Paging exists (the tool takes
    # an offset), so this bounds a single call rather than what the agent can ultimately
    # read — set to keep one oversized file from crowding out the rest of the turn.
    sandbox_files_max_read_lines: int = 2_000
    # Per-conversation live sandbox: a guest copy of the workspace in the platform VM,
    # created on the first code execution and kept so the agent can iterate (fix an error,
    # reuse an installed dependency) without rebuilding. Idle sessions are reaped to free
    # resources; a reap wipes the guest copy and leaves the workspace (the agent's files)
    # untouched on disk. `idle_ttl` is how long a session may sit unused before
    # it is killed; `reap_interval` is how often the reaper sweeps.
    sandbox_session_idle_ttl_s: float = 1800.0
    sandbox_session_reap_interval_s: float = 60.0
    # And how many conversations may hold one at once. The TTL bounds a session in time;
    # without a count, threads worked on in rotation all stay inside the window and every
    # one of them keeps a guest copy. Past the cap, the least-recently-used idle session is
    # reaped to make room — the same reap the TTL performs, triggered by pressure instead
    # of by the clock, and just as invisible to the conversation it hits.
    sandbox_max_sessions: int = 8
    # Live preview: the agent runs a dev server in the sandbox and the backend
    # reverse-proxies it to the frontend. How long to wait for that server to start
    # listening before reporting the start as failed (back to the agent).
    sandbox_preview_startup_timeout_s: float = 20.0
    # What a walk of a workspace leaves out, and **only** that — nothing here is ever
    # deleted. These names/globs are skipped when the workspace is reported (the model's
    # per-turn file block, a history snapshot) and when a delegated fork is compared and
    # merged back: a virtualenv is thousands of files the agent did not write, and
    # three-way merging a `.git` directory's internals is corruption rather than
    # continuity. Every one of them stays on disk regardless.
    #
    # Matched against every *part* of a path, so `build` covers `packages/x/build/` too.
    sandbox_walk_excludes: tuple[str, ...] = (
        ".venv",
        "venv",
        "env",
        ".local",
        ".tmp",
        ".home",
        "__pycache__",
        "node_modules",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".cache",
        "dist",
        "build",
        "*.pyc",
        "*.pyo",
        "*.egg-info",
    )

    # The one network allowlist, and the only one. Both fences read it: the VM's egress gateway,
    # the only route a sandbox command has off the guest, and the OS-level
    # confinement the code-mode shell and the approved host command run under. Two lists
    # would mean a hole in whichever one nobody remembered to widen. An entry matches that
    # host exactly — `github.com` is not `gist.github.com`; the subdomain form is spelled
    # `*.github.com`, which is why the seeds below name each host they need.
    #
    # Seeded with the package registries and source hosts, because the fence is tuned for
    # exfiltration and not for compute: installing a dependency and cloning a repository
    # are what a workspace is *for*, and an allowlist that made them feel dangerous would
    # only teach the operator to switch it off. Anything else is asked for per call and
    # approved per call (`services/egress.py`).
    egress_allowed_domains: tuple[str, ...] = (
        "pypi.org",
        "files.pythonhosted.org",
        "registry.npmjs.org",
        "registry.yarnpkg.com",
        "github.com",
        "api.github.com",
        "codeload.github.com",
        "objects.githubusercontent.com",
        "raw.githubusercontent.com",
        "deb.debian.org",
        "security.debian.org",
        "crates.io",
        "static.crates.io",
        "index.crates.io",
        "proxy.golang.org",
        "sum.golang.org",
        "huggingface.co",
        "cdn-lfs.huggingface.co",
    )

    # The approved host-command escape hatch, confined. Approval is consent to the
    # command the operator read, not to whatever it might reach afterwards, so the
    # process is additionally fenced at the OS level (seatbelt on macOS, bubblewrap on
    # Linux). Unlike the sandbox proper this degrades rather than fails closed: the
    # operator explicitly approved *this* command, and refusing to run it because a
    # platform primitive is missing would break the one case the tool exists for.
    host_command_sandbox_enabled: bool = True
    # Read-denied even under approval. The data directory is added to this at runtime
    # because it holds the vault, the sealed workspaces and the database: the agent must
    # never read its own encrypted store from the host, whatever it was approved to do.
    #
    # The rest of the list is the operator's *other* credentials, and it is deliberately
    # longer than the obvious four. The fence has a read denylist and no read allowlist,
    # so this is the only thing standing between a fenced command and a file outside the
    # worktree — and a command that reaches the network is a command that can send what it
    # read. Everything named here is a standing credential (a registry login, a cluster
    # token, a cloud session) or a shell history, which is where the ones nobody meant to
    # store end up.
    host_command_deny_read: tuple[str, ...] = (
        "~/.ssh",
        "~/.aws",
        "~/.gnupg",
        "~/.config/gh",
        "~/.netrc",
        "~/.docker",
        "~/.kube",
        "~/.config/gcloud",
        "~/.azure",
        "~/Library/Keychains",
        "~/.zsh_history",
        "~/.bash_history",
    )
    # Writes are **deny-by-default** under the confinement, so this list is what makes the
    # tool usable at all — an approved "change my host" command that cannot write anything
    # would fail confusingly rather than safely. Kept broad on purpose (the operator read
    # and approved this specific command); the fence's value is in the read denials and the
    # egress allowlist, which are the exfiltration paths. The credential paths above are
    # additionally write-denied, so a confined command can neither read nor clobber them.
    host_command_allow_write: tuple[str, ...] = ("~", "/tmp", "/var/tmp")
    # Extra writable paths (build caches) for the host shell's fence. Nothing reads this yet.
    host_shell_allow_write: tuple[str, ...] = (
        "~/.cache",
        "~/Library/Caches",
        "~/.npm/_cacache",
        "~/.bun/install/cache",
        "~/.cargo/registry",
        "~/.cargo/git",
        "~/go/pkg/mod",
    )

    # Meta-loop. The no-progress guard trips after this many identical tool
    # calls in a turn. The verifier (a post-turn judge + one bounded corrective
    # re-attempt) is off by default.
    loop_repeat_threshold: int = 3
    verify_enabled: bool = False
    # When the verifier is on, only judge turns that produced a checkable
    # artifact (made a tool call) — chitchat that called no tools is skipped.
    # Set False to judge every answer.
    verify_heuristic: bool = True

    # Approval grants. When the operator approves a deferred tool call with the
    # "allow for this conversation" option, that tool auto-approves in that
    # conversation for this long before lapsing back to strict per-call approval.
    approval_grant_ttl_s: float = 86400.0

    # Auto review (`services/permissions`). At the Auto level a deferred call is settled
    # by a deterministic shell judge and then, if that declines, by one structured call on
    # the utility model. `review_timeout_s` bounds that call — it runs inside a live turn
    # with the operator watching, so a stuck reviewer would hold the run open on a call
    # nobody has been asked about, which is worse than the park it was avoiding. A timeout
    # therefore parks, like every other way the review can fail. `review_max_tokens` is
    # the output cap, sized like the title call's: reasoning is requested off, but a
    # runtime that ignores the lever reasons anyway and the cap has to leave room for a
    # think block plus three short fields. `review_transcript_entries` is how much of the
    # thread the reviewer reads, counted from the end in **entries of prose** — the
    # operator's and the assistant's own words — rather than in messages, since a single
    # tool round trip is two messages and a window measured in those held nothing but tool
    # traffic after a few calls. The turn's opening request is always included on top of
    # it. `review_max_per_turn` caps how many *model* reviews one turn may spend: a
    # structurally-cleared command costs none, and past the cap the calls park.
    review_timeout_s: float = 30.0
    review_max_tokens: int = 2048
    review_transcript_entries: int = 12
    review_max_per_turn: int = 12

    # Web search. `web_search_result_limit` caps results from the SearXNG provider;
    # `web_search_timeout_s` bounds one query (its own budget, not the fetch timeout).
    web_search_result_limit: int = 10
    web_search_timeout_s: float = 15.0

    # Web fetch. The open web is treated as always-dynamic: every page is rendered in a
    # headless Chromium that runs in the platform VM (isolating untrusted page JS from the
    # host), driven over CDP; the rendered DOM is extracted to Markdown.
    # `enabled` turns fetch off (degrades); `startup_timeout_s` bounds browser bring-up;
    # `timeout_s` bounds navigation/render; `max_bytes` caps the rendered HTML/text handed
    # to the extractor; `wait_until` is Playwright's load milestone with `render_wait_ms`
    # an extra settle delay; `concurrency` bounds simultaneous contexts; `min_chars` is the
    # length below which extraction falls back to innerText. The browser is stealthed to
    # present as a normal user's Chrome (so sites return what a person would see):
    # `user_agent` empty ⇒ derive a realistic one from the engine version; `locale`/`timezone`
    # set the context. SSRF is enforced out-of-browser by the platform VM's egress
    # gateway, so every request is gated without the in-browser interception bot walls
    # detect.
    # `cookie_ttl_s` (>0 enables) caches a site's cookies in-memory across fetches so a
    # solved JS/bot challenge's clearance carries forward, bounded by TTL + `cookie_max`;
    # `min_interval_s` (>0 enables) is the minimum gap between fetches to the same host
    # (politeness vs rate limits); `challenge_waits`/`challenge_wait_ms` let a render that
    # looks like a bot-wall interstitial settle and be re-snapshotted instead of returned.
    web_fetch_enabled: bool = True
    web_fetch_startup_timeout_s: float = 45.0
    web_fetch_timeout_s: float = 15.0
    web_fetch_max_bytes: int = 2_000_000
    # An image the answer embeds, fetched by us so the operator's browser never
    # contacts the remote host (`services.webimage`). Its own budget, not the page
    # fetch's: no browser to start, so a slow image is a slow socket and nothing more,
    # and the cap is larger because a photograph is legitimately bigger than a page's
    # extracted text.
    web_image_timeout_s: float = 10.0
    web_image_max_bytes: int = 10_000_000
    web_fetch_wait_until: Literal["load", "domcontentloaded", "networkidle", "commit"] = (
        "domcontentloaded"
    )
    web_fetch_render_wait_ms: int = 250
    web_fetch_concurrency: int = 4
    web_fetch_min_chars: int = 200
    web_fetch_user_agent: str = ""
    web_fetch_locale: str = "en-US"
    web_fetch_timezone: str = "America/New_York"
    web_fetch_cookie_ttl_s: float = 1800.0
    web_fetch_cookie_max: int = 2000
    web_fetch_min_interval_s: float = 1.0
    web_fetch_challenge_waits: int = 2
    web_fetch_challenge_wait_ms: int = 5000
    # A fetched page's body is capped to this token budget before it reaches the model, so a
    # long article can't blow a local model's context in one tool result. The rest is reachable
    # by calling fetch again with the `offset` the truncation notice reports (paging).
    web_fetch_output_max_tokens: int = 4000
    # A `.pdf` URL can't render in the browser (it starts a download); the fetcher instead
    # downloads the bytes and extracts their text layer. `pdf_max_bytes` caps the download;
    # `pdf_max_pages` bounds extraction cost (no vision OCR — a scanned PDF degrades).
    web_fetch_pdf_max_bytes: int = 25_000_000
    web_fetch_pdf_max_pages: int = 50
    # Adaptive settle: a JS-heavy page (SPA) can report its shell first and fill the real
    # content a beat later. After the first snapshot, re-snapshot up to `settle_checks` times
    # (waiting `settle_wait_ms` each) while the page is still thin (< `settle_min_chars`) or
    # visibly still growing. A page that is already rich and stable pays zero extra waits.
    web_fetch_settle_checks: int = 3
    web_fetch_settle_wait_ms: int = 750
    web_fetch_settle_min_chars: int = 1000
    # Goal-aware distillation: when the fetch tool is given a `goal` and the page body is over
    # the output cap, the utility model distills it down to the goal-relevant content (verbatim
    # figures/tables preserved) instead of truncating. `window_tokens` is the per-call excerpt
    # size; `max_windows` caps coverage (8×12k ≈ 96k tokens of page); `timeout_s` bounds the
    # whole distillation (on timeout/failure it falls back to truncation + offset paging).
    web_fetch_distill_enabled: bool = True
    web_fetch_distill_window_tokens: int = 12000
    web_fetch_distill_max_windows: int = 8
    web_fetch_distill_timeout_s: float = 90.0

    # Browser control — the agent drives a real page (click, type, submit) rather than
    # only reading one, in a **visible Chromium on this machine** that the operator shares:
    # they can log in by hand before handing the thread over, or do one step themselves
    # faster than describing it. Nothing to switch on or off — the window is launched on
    # first use and closed with the app, so a thread that never browses opens nothing.
    # This is a second browser, separate from the headless one web fetch renders in
    # inside the platform VM; the SSRF proxy this one is pointed
    # at runs as a host subprocess instead, and no proxy means no browser.
    #
    # A session is conversation-scoped, not per-turn: a login or a half-filled form in
    # one turn must still be there in the next, which is the whole point of controlling a
    # browser rather than fetching pages. `idle_ttl_s` is how long one may sit unused
    # before it is reaped — the operator's own clicking counts as use, not just the
    # agent's tool calls — `reap_interval_s` how often the sweep runs, and `max_live` caps
    # how many conversations hold one at once, each of them a browser context in the VM
    # and a Playwright driver process, so this is deliberately small. `launch_timeout_s`
    # is how long an attach waits for the VM's browser to finish coming up.
    browser_control_idle_ttl_s: float = 900.0
    browser_control_reap_interval_s: float = 60.0
    browser_control_max_live: int = 3
    browser_control_launch_timeout_s: float = 30.0

    @property
    def browser_control_state_dir(self) -> Path:
        """Where the browser's shared login profile (cookies and local storage) is kept,
        on the host, between sessions."""
        return self.data_dir / "browser"

    # Managed web search. So search "just works" with zero operator setup, the
    # backend runs its own SearXNG in the platform VM and queries it automatically —
    # the DB-backed provider registry stays as an optional override for a custom/remote
    # instance. Without the VM web search simply degrades (no host fallback).
    # `searxng_base_url` points at an already-running instance instead, so nothing is
    # managed. Past `searxng_startup_timeout_s` a live but silent instance is reported as
    # slow and its output logged; bring-up keeps waiting either way.
    searxng_enabled: bool = True
    searxng_startup_timeout_s: float = 30.0
    searxng_base_url: str | None = None

    # Offline mode — when internet connectivity is lost the managed web services
    # (SearXNG + the fetch browser, both in the platform VM) are stopped to save resources and the
    # web tools are hidden from the agent; both return automatically when
    # connectivity is back. The operator can also force offline manually. Boot is
    # probe-first / fail-closed: one check decides whether the heavy browser starts
    # at all. The monitor TCP-connects to these public anchors (direct IPs, no DNS,
    # no payload) on its interval; `fail_threshold` consecutive failures declare
    # offline, `recover_threshold` consecutive successes declare online (hysteresis
    # so the services don't flap on a flaky link).
    # When False the monitor does no network probing and simply assumes online, so
    # auto-detection is off and only the manual switch can force offline (a host on a
    # known-always-online link, or tests that must not touch the network).
    offline_check_enabled: bool = True
    offline_auto_default: bool = True
    offline_check_interval_s: float = 30.0
    offline_check_timeout_s: float = 3.0
    offline_fail_threshold: int = 3
    offline_recover_threshold: int = 2
    offline_anchors: list[str] = ["1.1.1.1:443", "8.8.8.8:443", "9.9.9.9:443"]

    # Auto-titling: name a fresh thread from its first exchange (a best-effort
    # reasoning-off utility call). On by default; the operator can rename either way.
    # The auto path runs beside the answer and never holds the run open; its
    # `title_timeout_s` starts when the answer is written, because a one-slot server
    # queues the title behind it. `title_max_tokens` is the output cap: reasoning is
    # requested off, but a runtime that ignores the lever (e.g. LM Studio + Qwen)
    # reasons anyway, so the cap must leave room for a `<think>` block plus the title.
    # The manual re-title spans every operator turn (a longer think block) and is a
    # deliberate, awaited operator action, so it gets a wider budget and its own longer
    # timeout.
    title_enabled: bool = True
    title_timeout_s: float = 20.0
    title_max_tokens: int = 2048
    retitle_timeout_s: float = 60.0
    retitle_max_tokens: int = 4096

    # The per-conversation **work summary** — two or three sentences saying what the agent
    # did in a thread, shown in the re-entry band when the operator comes back to it
    # (`services/work_summaries.py`, `agent/work_summary.py`). Unlike the title, it is
    # written by a deferred background sweep rather than by the turn: an account of the work
    # is only worth writing once the work has stopped moving, and regenerating it per turn
    # would spend a utility-model call on a band nobody is looking at while they are still
    # typing in the thread.
    #
    # `work_summary_idle_minutes` is how long a thread must have been quiet before it is a
    # candidate — the *default*, overridden at runtime by the operator through
    # `PUT /chat/settings`, since how long a pause has to be before it counts as leaving is
    # a property of how somebody works and not of the deploy. 15 is the pause after which a
    # thread usually needs re-reading rather than just scrolling back.
    # `work_summary_sweep_interval_s` is only how often the query runs; it costs nothing
    # when nothing is stale, so it is well under the idle window rather than equal to it —
    # a sweep period as long as the window would make the band's arrival lag the operator's
    # return by up to a second window.
    # `work_summary_max_per_sweep` bounds the model calls one pass may start, so the first
    # sweep on a workspace full of old threads backfills over several minutes instead of
    # stampeding the background endpoint. `work_summary_timeout_s` and
    # `work_summary_max_tokens` are the titler's bounds for the titler's reason: the call
    # is best-effort, and a runtime that ignores the reasoning-off lever needs room for a
    # `<think>` block *and* the sentences behind it.
    work_summary_idle_minutes: int = 15
    work_summary_sweep_interval_s: float = 60.0
    work_summary_max_per_sweep: int = 5
    work_summary_timeout_s: float = 60.0
    work_summary_max_tokens: int = 4096

    # Uploads (UP-*). A file is accepted, stored encrypted at rest, and its text
    # extracted off the request path. `upload_max_bytes` caps a single file.
    # Uploads are rate-limited to protect the service (UP-4): a per-operator token
    # bucket holding `upload_rate_burst` tokens, refilling at `upload_rate_per_minute`.
    # `upload_extract_max_pages` bounds extraction cost — vision OCR is one model call
    # per scanned page — and pages beyond it are recorded as skipped, never dropped
    # silently. `upload_ocr_timeout_s` bounds a single page's vision call.
    upload_max_bytes: int = 50_000_000
    upload_rate_per_minute: float = 30.0
    upload_rate_burst: int = 10
    upload_extract_max_pages: int = 50
    upload_ocr_timeout_s: float = 120.0
    # Extraction engine. `auto` uses MinerU (high-fidelity Markdown — layout, tables,
    # formulas) when the `mineru` tool is detected on the host, falling back to the
    # zero-setup built-in (pypdfium2 text + vision OCR) otherwise; `mineru`/`basic`
    # pin one. MinerU is detected, not bundled, and runs as
    # a transient subprocess, so its heavy models load per-extraction and free on exit.
    # `upload_mineru_timeout_s` bounds one MinerU run before degrading to the built-in.
    upload_extractor: Literal["auto", "mineru", "basic"] = "auto"
    upload_mineru_timeout_s: float = 300.0

    # Conversation auto-compaction — the **only** context reduction that fires on measured
    # pressure rather than unconditionally. Once a thread's *projected* footprint (what is
    # already in the replay plus the prompt and context about to be added) reaches
    # `auto_compact_threshold` of the model's context window, everything since the newest
    # checkpoint is summarized by the thread's own main model — the conversation continuing,
    # so a local engine reuses the prefix it already holds — into one new checkpoint, and the thread
    # carries on from that summary alone — no turns are retained verbatim beneath it.
    # The threshold is 0.80, not the old 0.95: at 95% the fold leaves no room for the turn
    # that triggered it, so the very next request overflows anyway. Nothing is deleted:
    # the operator's transcript keeps every turn, and only what is re-sent to the model
    # shrinks. It fires **between** turns, in the orchestrator prelude,
    # so it can never disturb reasoning already in flight — with one exception: a provider
    # context-overflow inside a turn folds once and retries the failed request.
    # There is deliberately **no** output budget and **no** timeout for the summary: it replaces the
    # thread, so it runs until the model finishes, and the fold holds the inactivity
    # watchdog open while it does. Enabled/threshold are the *defaults* — the
    # operator overrides them at runtime via `PUT /chat/settings`, and enablement per thread
    # via `/conversations/{id}`.
    auto_compact_enabled: bool = True
    auto_compact_threshold: float = 0.80

    # What the footprint estimator assumes the per-turn overhead costs — instructions,
    # system prompt and tool schemas — when a thread carries no measured `TurnOverhead`
    # record (every turn taken before the record existed, and the first turn of a fresh
    # thread). The assembled catalog measures at ~14k tokens, so the alternative default,
    # zero, would tell the compaction trigger a nearly-full thread had room to spare.
    # Deliberately under the measured figure: the estimate is a fallback, and it should
    # nudge the trigger early without folding a thread that was never under pressure.
    context_overhead_fallback_tokens: int = 12000

    # Anthropic prompt caching (services/providers/anthropic.py): the TTL requested on the
    # cache breakpoints the adapter sets over the instructions and the tool definitions —
    # the two prefix segments that are byte-identical across every turn of a thread. "5m"
    # is Anthropic's standard tier and covers back-to-back turns; "1h" costs more to write
    # and pays off only when the operator returns to a thread after a long pause.
    # "off" sends no breakpoints at all — the escape hatch for an Anthropic-compatible
    # proxy that rejects `cache_control` outright, where the choice is between paying full
    # price for every prefix and not reaching the endpoint at all.
    anthropic_cache_ttl: Literal["5m", "1h", "off"] = "5m"

    # OpenAI prompt-cache retention (services/providers/openai_compat.py). OpenAI caches a
    # matching prefix implicitly with no request field at all; this asks it to hold that
    # prefix *longer* than the default five-to-ten minutes — "24h" for a thread an operator
    # returns to, "in_memory" for the standard behaviour stated explicitly.
    #
    # **"off" is the default and sends nothing**, which is not timidity: this adapter fronts
    # every OpenAI-shaped server there is, and the great majority of them are local engines
    # that have never heard of the field. The default within-session case is already covered
    # by OpenAI's own retention, so the field buys nothing on the endpoints that understand
    # it either, until an operator says they want the long tier. Off also declares the three
    # cache fields unsupported on the profile, so the library strips one a caller supplies
    # per request rather than sending it to a server that may reject the whole request.
    #
    # Deliberately not a knob for `prompt_cache_key` or `prompt_cache_options`: the first
    # only affects which cache shard a request routes to and is worth nothing on a local
    # engine, and the second is meaningful on two hosted model families that can only be
    # recognised here by *name* — which for an adapter that fronts any server is a
    # coincidence of what the operator typed.
    openai_cache_retention: Literal["in_memory", "24h", "off"] = "off"


@lru_cache
def get_settings() -> Settings:
    return Settings()
