"""Application configuration.

Deploy-level secrets and defaults come from the environment / ``.env`` before
first boot. Runtime-mutable user settings live in the DB and are
not modeled here.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Literal

from pydantic import Field, field_validator
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

    # localhost and 127.0.0.1 are distinct browser origins, so both are listed.
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    auth_enabled: bool = True

    # All user data lives under here — gitignored, encrypted at rest.
    data_dir: Path = Path("data")

    @field_validator("data_dir")
    @classmethod
    def _absolute_data_dir(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    # Outside `data_dir` on purpose: the host-command fence (`services/sandbox/fence.py`)
    # denies reads of the whole data directory, so a worktree there would be unreadable by
    # the shell that builds and tests it. A worktree is plaintext on disk, as is the
    # operator's own repository it checks out.
    worktrees_dir: Path = Path("~/.hephaestus/worktrees")

    @field_validator("worktrees_dir")
    @classmethod
    def _absolute_worktrees_dir(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    # None ⇒ a file under data_dir; tests pass an in-memory URL.
    db_url: str | None = None
    # With auth disabled, the vault is set up or unlocked at boot with this; with auth
    # enabled the operator unlocks via login.
    unlock_passphrase: str | None = None

    # Run substrate bounds. Timeouts are seconds; None disables.
    #
    # Concurrency is split into lanes (`runs/lanes.py`) so the operator's turn never queues
    # behind a scheduled task or an agent-opened thread. `run_max_concurrency` is the host
    # ceiling across every lane; the two unattended lanes are capped well below it because
    # their failure mode is volume, which leaves slots the operator can always take.
    run_max_concurrency: int = 8
    run_background_concurrency: int = 2
    # Routine runs, reserved on top of `run_max_concurrency`: they neither wait for the
    # operator's slots nor take them.
    run_routine_concurrency: int = 2
    # Also how many sub-agents execute at once (they run in the linked lane). The lane's
    # narrowness guarantees the operator a floor of slots, so a burst queues instead.
    # `chat.subagent_max_concurrent` is separate: it caps how many may exist.
    run_linked_concurrency: int = 3
    # Off by default: it mostly fires on legitimately slow runs (a local model, a long
    # sandboxed call). Opt-in for the gap the inactivity watchdog leaves, since a run that
    # keeps emitting refreshes that clock on every frame.
    run_wall_clock_timeout_s: float | None = None
    run_inactivity_timeout_s: float | None = 120.0
    # How long a model request may stream nothing before its first part, with the
    # inactivity clock held open meanwhile. Reading a long prompt is silent: about 2s per
    # 1k new tokens for a 27B model on Apple silicon, ~100s for a cold 50k-token thread.
    first_token_timeout_s: float | None = 600.0
    # One tool call's bound. A call holds the inactivity clock open, so this stops one that wedges.
    tool_call_timeout_s: float | None = 1800.0
    # Per-drain bound at shutdown (queue flush, run unwind, browser close). A wait on a
    # stuck job would force a second Ctrl+C, which skips the rest of the shutdown.
    shutdown_drain_timeout_s: float = 5.0

    # Models come only from the DB-backed registry (services/registry.py), not the env.

    # A turn has no request or tool-call budget; the loop guard and the timeouts bound it.
    # Re-sends of a request a transient model error cut short (dropped connection, 5xx, 429,
    # mid-call stream end).
    auto_continue_transient_max: int = 2
    # Retries of a tool call a tool rejected, and of an answer that failed output validation.
    agent_tool_retries: int = 3
    agent_output_retries: int = 2

    # `run_code` — the in-process Monty sandbox a model-written script calls tools from
    # (`agent/code_mode.py`). Duration and memory caps are per snippet; the call cap is per script.
    code_mode_max_duration_s: float = 30.0
    code_mode_max_memory_mb: int = 256
    code_mode_max_tool_calls: int = 25

    # A tool result longer than this many characters is saved to the thread's scratch and
    # the model gets a head-and-tail preview with the path (`tools/spill.py`).
    tool_output_spill_chars: int = Field(default=40_000, ge=1000)

    # A sub-agent turn's wall clock, the one on by default: nobody watches it, and the
    # inactivity watchdog can't end a model that keeps streaming. Eighteen minutes fits a
    # thorough read of a dozen sources, not a stuck one.
    subagent_wall_clock_timeout_s: float | None = 1080.0

    # How deep a delegation tree may grow: a thread's own agents are depth 1. An agent's
    # definition may lower it for the agents below it, never raise it.
    agent_max_depth: int = 3

    # The VMs (smol machines): one per conversation for its commands, and one for headless
    # Chromium and SearXNG, booted from an image built by `backend/vm/build_image.sh` (or
    # `vm_image`, a prebuilt archive). Without them, dependent capabilities report
    # unavailable (never a host fallback). `vm_ca_bundle` is an extra CA bundle the image
    # trusts, for TLS-inspecting networks.
    vm_enabled: bool = True
    vm_image: str | None = None
    vm_ca_bundle: str | None = None
    vm_services_cpus: int = 2
    vm_services_memory_mb: int = 2048

    # Each conversation's machine, created on its first command. A reap deletes the
    # machine and leaves the files it mounted. Past `sandbox_max_sessions` machines, the
    # least-recently-used idle one is reaped.
    sandbox_enabled: bool = True
    sandbox_cpus: int = 2
    sandbox_memory_mb: int = 4096
    # Bounds one `files_read_file` call (the tool pages by offset), so one oversized file
    # can't crowd out the turn.
    sandbox_files_max_read_lines: int = 2_000
    sandbox_session_idle_ttl_s: float = 1800.0
    sandbox_session_reap_interval_s: float = 60.0
    sandbox_max_sessions: int = 8
    # How long the backend waits for a previewed dev server to listen before reporting failure.
    sandbox_preview_startup_timeout_s: float = 20.0
    # Skipped when a workspace is reported (per-turn file block, history snapshot) and when
    # a delegated fork is compared and merged back: a virtualenv is files the agent didn't
    # write, and three-way merging `.git` internals corrupts. Nothing is ever deleted.
    # Matched against every part of a path, so `build` covers `packages/x/build/` too.
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

    # The single network allowlist, read by both fences: each conversation VM's
    # `allow_hosts` and the OS-level confinement of `shell_run_on_host`. A second list
    # would leave a hole in whichever one wasn't widened. The host fence matches entries
    # exactly (`*.github.com` for subdomains); smol's VM allowlist can only match a name
    # with its subdomains, so in a VM `github.com` also admits `gist.github.com`.
    #
    # Seeded with package registries and source hosts: installing and cloning are what a
    # workspace is for, and the fence targets exfiltration. Anything else is asked for and
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
        # The guest image is Ubuntu: ports serves arm64, archive and security serve amd64.
        "ports.ubuntu.com",
        "archive.ubuntu.com",
        "security.ubuntu.com",
        "crates.io",
        "static.crates.io",
        "index.crates.io",
        "proxy.golang.org",
        "sum.golang.org",
        "huggingface.co",
        "cdn-lfs.huggingface.co",
    )

    # The approved host-command escape hatch, confined. Approval covers the command the
    # operator read, not what it might reach, so it is also fenced at the OS level
    # (seatbelt on macOS, bubblewrap on Linux). Unlike the sandbox this degrades rather
    # than failing closed: refusing an explicitly approved command over a missing platform
    # primitive would break the tool's one use.
    host_command_sandbox_enabled: bool = True
    # Applies to `shell_run_on_host` only; the host shell reads everything but the floor
    # (`services/sandbox/floor.py`), and write-denies this list.
    # Read-denied even under approval. The data directory is added at runtime (it holds the
    # vault, sealed workspaces and database).
    #
    # The rest is the operator's other credentials (registry logins, cluster tokens, cloud
    # sessions) and shell histories. The fence has no read allowlist, so this list is all
    # that stands between a fenced command and a file outside the worktree, and a command
    # with network access can send what it read.
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
    # Writes are deny-by-default under the confinement, so this list makes the tool usable.
    # Kept broad because the operator approved the specific command; the fence's value is
    # the read denials and egress allowlist (the exfiltration paths). The credential paths
    # above are also write-denied.
    host_command_allow_write: tuple[str, ...] = ("~", "/tmp", "/var/tmp")

    # Meta-loop. The no-progress guard trips after this many identical tool calls in a
    # turn. The verifier (post-turn judge + one bounded corrective re-attempt) is off by default.
    loop_repeat_threshold: int = 3
    verify_enabled: bool = False
    # Judge only turns that made a tool call (a checkable artifact); False judges every answer.
    verify_heuristic: bool = True

    # How long "allow for this conversation" auto-approves a tool before lapsing back to
    # per-call approval.
    approval_grant_ttl_s: float = 86400.0

    # Auto review (`services/permissions`). A deferred call the structural judge declines
    # goes to the `decision` model. A timeout parks the call, like every other failure.
    # `review_transcript_entries` counts entries of prose (the operator's and the
    # assistant's own words), not messages; the turn's opening request is kept on top of
    # it. `review_transcript_chars` caps them all together, and with
    # `review_ledger_entries` (how many of the conversation's approved calls the model
    # reads) bounds a review's time: a decision model re-reads the prompt for every
    # question. `review_max_per_turn` caps decision calls per turn; past it the calls park.
    # Both timeouts cover a local server loading the model on first request; the bind
    # probe also waits out a cold load from disk.
    review_timeout_s: float = 30.0
    review_probe_timeout_s: float = 120.0
    review_transcript_entries: int = 12
    review_transcript_chars: int = 2_000
    review_max_per_turn: int = 12
    review_ledger_entries: int = 3
    # Thresholds over the decision model's probabilities (`decision_scoring.py`).
    # `too_destructive` at P(irreversible) >= destroy_min. `low` needs at most
    # low_tail_max on the two upper levels, at most low_irreversible_max on the top one,
    # and P(outward) < outward_max. `explicitly_yes` needs P(requested) >= requested_min,
    # P(refused) < refused_clear_max and P(mismatch) < mismatch_max.
    decision_destroy_min: float = 0.35
    decision_low_tail_max: float = 0.2
    decision_low_irreversible_max: float = 0.1
    decision_outward_max: float = 0.5
    decision_refused_min: float = 0.5
    decision_requested_min: float = 0.8
    decision_refused_clear_max: float = 0.2
    decision_mismatch_max: float = 0.5

    # Web search. `web_search_result_limit` caps results from the SearXNG provider;
    # `web_search_timeout_s` bounds one query (its own budget, not the fetch timeout).
    web_search_result_limit: int = 10
    web_search_timeout_s: float = 15.0

    # Web fetch. Every page is rendered in a headless Chromium in the services VM (isolating
    # page JS from the host), driven over CDP, and the DOM is extracted to Markdown.
    # `enabled` off degrades; `startup_timeout_s` bounds browser bring-up; `timeout_s`
    # bounds navigation/render; `max_bytes` caps the HTML/text handed to the extractor;
    # `wait_until` is Playwright's load milestone plus `render_wait_ms` of settle delay;
    # `concurrency` bounds simultaneous contexts; `min_chars` is the length below which
    # extraction falls back to innerText. The browser presents as a normal Chrome:
    # `user_agent` empty ⇒ derived from the engine version; `locale`/`timezone` set the
    # context. SSRF is bounded by the services VM reaching public addresses only, so no
    # in-browser interception that bot walls detect.
    # `cookie_ttl_s` (>0 enables) caches cookies in memory so a solved bot challenge carries
    # forward, bounded by TTL and `cookie_max`; `min_interval_s` (>0 enables) is the minimum
    # gap between fetches to one host; `challenge_waits`/`challenge_wait_ms` let a render
    # that looks like a bot-wall interstitial settle and be re-snapshotted.
    web_fetch_enabled: bool = True
    web_fetch_startup_timeout_s: float = 45.0
    web_fetch_timeout_s: float = 15.0
    web_fetch_max_bytes: int = 2_000_000
    # Images the answer embeds, fetched by us so the operator's browser never contacts the
    # remote host (`services.webimage`). Separate budget from page fetch: no browser, and
    # photographs are bigger than extracted text.
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
    # Token cap on a fetched page's body so one result can't blow a local model's context;
    # the rest is reachable via the `offset` the truncation notice reports.
    web_fetch_output_max_tokens: int = 4000
    # A `.pdf` URL starts a download rather than rendering, so the fetcher downloads it and
    # extracts the text layer. `pdf_max_bytes` caps the download; `pdf_max_pages` bounds
    # extraction (no OCR; a scanned PDF degrades).
    web_fetch_pdf_max_bytes: int = 25_000_000
    web_fetch_pdf_max_pages: int = 50
    # Adaptive settle for SPAs that fill content after the shell: re-snapshot up to
    # `settle_checks` times (`settle_wait_ms` apart) while the page is thin
    # (< `settle_min_chars`) or still growing. A rich, stable page pays no extra waits.
    web_fetch_settle_checks: int = 3
    web_fetch_settle_wait_ms: int = 750
    web_fetch_settle_min_chars: int = 1000
    # Goal-aware distillation: with a `goal` and a body over the output cap, the utility
    # model distills goal-relevant content (verbatim figures/tables kept) instead of
    # truncating. `window_tokens` is the per-call excerpt size; `max_windows` caps coverage
    # (8×12k ≈ 96k tokens); `timeout_s` bounds the whole run, after which it falls back to
    # truncation and offset paging.
    web_fetch_distill_enabled: bool = True
    web_fetch_distill_window_tokens: int = 12000
    web_fetch_distill_max_windows: int = 8
    web_fetch_distill_timeout_s: float = 90.0

    # Browser control — the agent clicks, types and submits in a visible Chromium on this
    # machine that the operator shares (they can log in by hand first). It launches on first
    # use and closes with the app; there is no switch. It is separate from web fetch's
    # headless browser in the VM; its SSRF proxy runs as a host subprocess, and no proxy
    # means no browser.
    #
    # Sessions are conversation-scoped so a login or half-filled form survives across
    # turns. `idle_ttl_s` is the unused time before a reap (the operator's own clicking
    # counts as use); `reap_interval_s` is the sweep period; `max_live` caps conversations
    # holding one (each is a VM browser context plus a Playwright driver process, so keep it
    # small); `launch_timeout_s` is how long an attach waits for the VM's browser.
    browser_control_idle_ttl_s: float = 900.0
    browser_control_reap_interval_s: float = 60.0
    browser_control_max_live: int = 3
    browser_control_launch_timeout_s: float = 30.0

    @property
    def browser_control_state_dir(self) -> Path:
        """Host directory holding the browser's shared login profile between sessions."""
        return self.data_dir / "browser"

    # Managed web search: the backend runs its own SearXNG in the services VM, with the
    # DB-backed provider registry as an optional override. Without the VM, search degrades
    # (no host fallback). `searxng_base_url` points at an existing instance instead, so
    # nothing is managed. Past `searxng_startup_timeout_s` a silent instance is reported
    # slow and its output logged; bring-up keeps waiting.
    searxng_enabled: bool = True
    searxng_startup_timeout_s: float = 30.0
    searxng_base_url: str | None = None

    # Offline mode — on lost connectivity the managed web services (SearXNG and the fetch
    # browser, in the VM) stop and the web tools are hidden; both return with connectivity.
    # The operator can force offline manually. Boot is probe-first and fail-closed: one
    # check decides whether the heavy browser starts. The monitor TCP-connects to these
    # public anchors (direct IPs, no DNS, no payload) each interval; `fail_threshold`
    # consecutive failures declare offline and `recover_threshold` successes declare online
    # (hysteresis against flapping). When `offline_check_enabled` is False it never probes
    # and assumes online, so only the manual switch can force offline.
    offline_check_enabled: bool = True
    offline_auto_default: bool = True
    offline_check_interval_s: float = 30.0
    offline_check_timeout_s: float = 3.0
    offline_fail_threshold: int = 3
    offline_recover_threshold: int = 2
    offline_anchors: list[str] = ["1.1.1.1:443", "8.8.8.8:443", "9.9.9.9:443"]

    # Auto-titling: a best-effort reasoning-off utility call naming a fresh thread from its
    # first exchange. It runs beside the answer and never holds the run open; its
    # `title_timeout_s` starts when the answer is written, since a one-slot server queues
    # the title behind it. `title_max_tokens` leaves room for a `<think>` block plus the
    # title, as some runtimes (e.g. LM Studio + Qwen) ignore reasoning-off. The manual
    # re-title spans every operator turn and is awaited, so it gets wider bounds.
    title_enabled: bool = True
    title_timeout_s: float = 20.0
    title_max_tokens: int = 2048
    retitle_timeout_s: float = 60.0
    retitle_max_tokens: int = 4096

    # The per-conversation work summary: two or three sentences on what the agent did,
    # shown in the re-entry band (`services/work_summaries.py`, `agent/work_summary.py`).
    # A deferred background sweep writes it, not the turn, so it isn't regenerated while
    # the operator is still typing.
    #
    # `work_summary_idle_minutes` is the quiet time before a thread is a candidate; a
    # default the operator overrides via `PUT /chat/settings`, since what counts as leaving
    # depends on how they work. `work_summary_sweep_interval_s` is only the query period
    # (free when nothing is stale); keep it well under the idle window or the band lags the
    # operator's return. `work_summary_max_per_sweep` bounds model calls per pass so the
    # first sweep over old threads backfills instead of stampeding the background endpoint.
    # Timeout and max tokens follow the titler: best-effort, with room for a `<think>`
    # block plus the sentences.
    work_summary_idle_minutes: int = 15
    work_summary_sweep_interval_s: float = 60.0
    work_summary_max_per_sweep: int = 5
    work_summary_timeout_s: float = 60.0
    work_summary_max_tokens: int = 4096

    # Uploads: stored encrypted, text extracted off the request path. `upload_max_bytes`
    # caps one file. Rate limit is a per-operator token bucket of `upload_rate_burst`
    # tokens refilling at `upload_rate_per_minute`. `upload_extract_max_pages` bounds
    # extraction (vision OCR is a model call per scanned page); pages beyond it are
    # recorded as skipped. `upload_ocr_timeout_s` bounds one page's vision call.
    upload_max_bytes: int = 50_000_000
    upload_rate_per_minute: float = 30.0
    upload_rate_burst: int = 10
    upload_extract_max_pages: int = 50
    upload_ocr_timeout_s: float = 120.0
    # `auto` uses MinerU (layout, tables, formulas) when detected on the host, else the
    # built-in (pypdfium2 text + vision OCR); `mineru`/`basic` pin one. MinerU is not
    # bundled and runs as a transient subprocess, so its models load per extraction.
    # `upload_mineru_timeout_s` bounds one run before degrading to the built-in.
    upload_extractor: Literal["auto", "mineru", "basic"] = "auto"
    upload_mineru_timeout_s: float = 300.0

    # Conversation auto-compaction, the only context reduction triggered by measured
    # pressure. When a thread's projected footprint (replay plus the prompt and context
    # about to be added) reaches `auto_compact_threshold` of the context window, everything
    # since the newest checkpoint is summarized by the thread's own main model (so a local
    # engine reuses its prefix) into a new checkpoint, and the thread continues from that
    # summary alone. 0.80 because at 0.95 the fold leaves no room for the turn that
    # triggered it. The transcript keeps every turn; only what is re-sent shrinks. It fires
    # between turns, in the orchestrator prelude, except that a provider context overflow
    # mid-turn folds once and retries the request. The summary has no output budget or
    # timeout because it replaces the thread; the fold holds the inactivity watchdog open.
    # Enabled/threshold are defaults the operator overrides via `PUT /chat/settings`
    # (per-thread enablement via `/conversations/{id}`).
    auto_compact_enabled: bool = True
    auto_compact_threshold: float = 0.80
    # What a fold copies in verbatim besides the briefing: the operator's own messages,
    # newest first up to this many tokens, and then the thread's most recently read files
    # re-read fresh, each and all together capped. They count toward the new footprint, so
    # their sum must stay well under the room the threshold leaves.
    compaction_operator_messages_tokens: int = 20_000
    compaction_restore_files: int = 5
    compaction_restore_file_tokens: int = 8_000
    compaction_restore_total_tokens: int = 25_000

    # Per-turn overhead (instructions, system prompt, tool schemas) the footprint estimator
    # assumes when a thread has no measured `TurnOverhead` record. The catalog measures
    # ~14k tokens; zero would tell the trigger a nearly-full thread had room. Set under the
    # measured figure so it nudges early without folding a thread that was never pressured.
    context_overhead_fallback_tokens: int = 12000

    # Anthropic prompt caching (services/providers/anthropic.py): TTL on the cache
    # breakpoints over the instructions and tool definitions, the prefix segments identical
    # across a thread's turns. "5m" covers back-to-back turns; "1h" costs more to write and
    # pays off only after long pauses. "off" sends no breakpoints, for an
    # Anthropic-compatible proxy that rejects `cache_control`.
    anthropic_cache_ttl: Literal["5m", "1h", "off"] = "5m"

    # OpenAI prompt-cache retention (services/providers/openai_compat.py). OpenAI caches a
    # matching prefix implicitly; this asks it to hold the prefix longer than the default
    # five to ten minutes ("24h"), or states the standard behaviour ("in_memory").
    #
    # "off" is the default and sends nothing: this adapter fronts every OpenAI-shaped
    # server, mostly local engines that don't know the field. Off also declares the three
    # cache fields unsupported on the profile, so the library strips any a caller supplies
    # instead of sending them to a server that may reject the request.
    #
    # No knob for `prompt_cache_key` or `prompt_cache_options`: the first only picks a cache
    # shard (worthless locally), and the second applies to two hosted model families that
    # this adapter could only recognise by name.
    openai_cache_retention: Literal["in_memory", "24h", "off"] = "off"


def get_settings() -> Settings:
    return Settings.settings_cached()
