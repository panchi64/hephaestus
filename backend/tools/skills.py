"""Skill tools — the agent opens a playbook and works from it (`SKILL-2`, `SKILL-3`).

Skills follow the **Agent Skills** standard's progressive disclosure, and these tools are
levels two and three of it:

1. The turn's instructions list the platform's own skills, say how many others there are
   (stored, the project's, and plugins'), and list the pinned ones; the rest are found by
   name and description through ``platform_search``.
2. ``open`` returns the skill's full ``SKILL.md`` instructions — the level the model pays for
   only when a skill is actually relevant.
3. ``open`` **also stages the bundle into the run's workspace** under ``skills/{name}/``,
   so the supporting files are real files: references to read, scripts to run. *Which*
   workspace comes from the resolver (``tools/workspace.py``) — the conversation's sandbox
   in a sandbox thread, the project's git worktree in a code one — so a skill's scripts are
   always somewhere this run can actually execute them. In a sandbox thread that means
   nothing about a skill ever touches the host; in a worktree thread they land in the
   worktree, in the git-ignored directory the run's own scratch lives in.

``create`` and ``edit`` let the agent write skills down as it learns them. ``create``
always writes a **draft**: publishing is the operator's act, and it is what makes a skill
visible to the model at all. That split is also the seam `SKILL-4` (auto-publishing
high-confidence recoveries) will plug into — and it is why ``create`` is not marked
sensitive while ``edit``, which rewrites something already published and therefore
already being followed, is (`AE-3`).

Thin like every tool here: the format rules, sealing, and validation live in
``services/skills``; a missing capability degrades to a message the model can act on.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic_ai import FunctionToolset, ModelRetry, RunContext

from core.diagnostics import render_tool_error, suggest
from core.exceptions import NotFoundError, SkillSpanError, SkillValidationError
from prompts.agent import (
    SKILL_CATALOG,
    SKILL_PINNED,
    SKILL_PINNED_LIMIT,
    SKILL_PLATFORM,
    SKILL_SAVED,
)
from services.plugins import PluginSkill, PluginStore
from services.projects.worktree import WorktreeBusyError
from services.sandbox import SandboxError
from services.skills import (
    BUNDLE_MAX_BYTES,
    SKILL_FILE,
    SkillCatalogEntry,
    SkillStore,
    render_skill_md,
)
from services.skills.bundle import ParsedSkill
from services.skills.platform import PlatformSkills
from services.skills.sources import DiskSkill, project_skills
from services.workspace import RunWorkspace

from .deps import RunDeps
from .project_agents import project_tree
from .workspace import run_workspace

#: Where a staged bundle lands inside the sandbox working directory (mirrors
#: ``attachments/`` from the attachments tool).
_STAGE_DIR = "skills"
_UNAVAILABLE = "Skills are unavailable right now."
#: How many runs' project skills are held at once, sized for the runs executing concurrently.
_MAX_CACHED_RUNS = 32

_project_cache: OrderedDict[str, tuple[DiskSkill, ...]] = OrderedDict()


@dataclass(frozen=True)
class _Resolved:
    skill: ParsedSkill
    #: Set for a stored skill, whose files are read from the store.
    stored_id: str | None
    files: tuple[tuple[str, bytes], ...]


async def skill_catalog(ctx: RunContext[RunDeps]) -> list[SkillCatalogEntry]:
    """Every skill this run can open, by precedence: stored, then the project's, then the
    platform's, then those published plugins contribute. A name is listed once, from the
    source that wins it."""
    store = ctx.deps.caps.get_optional(SkillStore)
    entries = await store.catalog(ctx.deps.owner_id) if store is not None else []
    taken = {entry.name for entry in entries}
    for source, found in (
        ("project", await _project_skills(ctx)),
        ("platform", await _platform_skills(ctx)),
        ("plugin", await _plugin_skills(ctx)),
    ):
        for entry in found:
            if entry.skill.name not in taken:
                taken.add(entry.skill.name)
                entries.append(
                    SkillCatalogEntry(entry.skill.name, entry.skill.description, source=source)
                )
    return entries


async def _resolve(ctx: RunContext[RunDeps], name: str) -> _Resolved | None:
    store = ctx.deps.caps.get_optional(SkillStore)
    if store is not None:
        try:
            view = await store.get_by_name(ctx.deps.owner_id, name, published_only=True)
        except NotFoundError:
            pass
        else:
            return _Resolved(view.parsed(), view.id, ())
    for found in (
        await _project_skills(ctx),
        await _platform_skills(ctx),
        await _plugin_skills(ctx),
    ):
        for entry in found:
            if entry.skill.name == name:
                return _Resolved(entry.skill, None, tuple(entry.files))
    return None


async def _platform_skills(ctx: RunContext[RunDeps]) -> tuple[DiskSkill, ...]:
    platform = ctx.deps.caps.get_optional(PlatformSkills)
    return await platform.enabled(ctx.deps.owner_id) if platform is not None else ()


async def _plugin_skills(ctx: RunContext[RunDeps]) -> list[PluginSkill]:
    plugins = ctx.deps.caps.get_optional(PluginStore)
    return await plugins.skills(ctx.deps.owner_id) if plugins is not None else []


async def _project_skills(ctx: RunContext[RunDeps]) -> tuple[DiskSkill, ...]:
    """The bound project's skills, read once per run."""
    run_id = ctx.deps.run.id
    cached = _project_cache.pop(run_id, None)
    if cached is None:
        root = await project_tree(ctx)
        cached = project_skills(root) if root is not None else ()
        if len(_project_cache) >= _MAX_CACHED_RUNS:
            _ = _project_cache.popitem(last=False)
    _project_cache[run_id] = cached
    return cached


async def _stage(
    workspace: RunWorkspace, skill: ParsedSkill, files: list[tuple[str, bytes]]
) -> list[str]:
    """Write the bundle into the run's workspace, skipping any file already there
    byte-for-byte so re-opening a skill in a warm session is a no-op rather than a
    rewrite. Returns the absolute paths the model should use — which is why they come
    from the workspace rather than a literal ``/work``: a worktree run's files are on the
    host, and telling the model about a sandbox path it cannot reach is worse than not
    staging at all."""
    root = f"{workspace.stage_prefix}{_STAGE_DIR}/{skill.name}"
    staged: list[str] = []
    for relpath, blob in [(SKILL_FILE, render_skill_md(skill).encode())] + files:
        target = f"{root}/{relpath}"
        try:
            if workspace.files.read_file(target) == blob:
                staged.append(workspace.display(target))
                continue
        except SandboxError:
            pass  # not staged yet — write it
        workspace.files.write_file(target, blob)
        staged.append(workspace.display(target))
    return staged


def skills_toolset() -> FunctionToolset[RunDeps]:
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool
    async def open(ctx: RunContext[RunDeps], name: str) -> dict[str, object]:
        """Open one of the skills listed in your instructions and follow it.

        The listing only says a skill exists; this returns the procedure, so call it
        before starting a task a skill covers. ``name`` must match the listing exactly.

        Returns ``instructions`` (the skill's full text, guidance for this task) and
        ``files`` — its bundled files, staged under ``skills/{name}/``. **Use the returned
        paths verbatim**; where they live depends on the kind of conversation. Invoke a
        bundled script explicitly (``python …/scripts/y.py``), since staged files are not
        marked executable.

        ``allowed_tools``, when present, is the author's advisory note about which tools
        the skill expects to use: it neither restricts you nor grants you anything."""
        resolved = await _resolve(ctx, name)
        if resolved is None:
            names = [e.name for e in await skill_catalog(ctx)]
            close = suggest(name, names)
            raise ModelRetry(
                render_tool_error(
                    f"no skill named {name!r}",
                    hints=[
                        *([close] if close else []),
                        f"available skills: {', '.join(names) or 'none yet'}",
                    ],
                )
            )
        skill = resolved.skill

        result: dict[str, object] = {
            "ok": True,
            "name": skill.name,
            "description": skill.description,
            "instructions": skill.body,
            "files": [],
        }
        if skill.allowed_tools:
            result["allowed_tools"] = skill.allowed_tools

        try:
            workspace = await run_workspace(ctx)
        except WorktreeBusyError:
            workspace = None
        if workspace is None:
            result["note"] = (
                "Your computer is unavailable, so this skill's bundled files were not "
                "staged — follow the instructions without them."
            )
            return result

        store = ctx.deps.caps.get_optional(SkillStore)
        files = (
            await store.file_contents(ctx.deps.owner_id, resolved.stored_id)
            if store is not None and resolved.stored_id is not None
            else list(resolved.files)
        )
        total = sum(len(blob) for _, blob in files)
        if total > BUNDLE_MAX_BYTES:
            result["note"] = (
                "This skill's bundle is too large to stage; follow the instructions without "
                "its files."
            )
            return result
        try:
            result["files"] = await _stage(workspace, skill, files)
        except SandboxError as exc:
            result["note"] = f"The skill's files could not be staged: {exc}"
        return result

    # Ungated on purpose, and the *only* skill write that is: `create` can produce
    # nothing but a draft, and a draft reaches the model exactly never — `open` and
    # `catalog` are both `published_only`. Publishing is the operator's own act, so their
    # review already stands where an approval prompt would. Gating this would ask them the
    # same question twice.
    @toolset.tool
    async def create(
        ctx: RunContext[RunDeps], name: str, description: str, body: str
    ) -> dict[str, object]:
        """Write down a reusable procedure you worked out, so it is available in future
        conversations.

        Use it for something non-obvious that will recur — a multi-step recovery, a fiddly
        setup, a checklist that worked — written the way a skill reads: **when** to use it,
        **how**, the **pitfalls**, and **how to verify** it worked.

        ``name`` is the skill's id: lowercase letters, numbers and hyphens.
        ``description`` is the one line that decides whether a future turn opens it, so say
        what it does *and* when it applies.

        It saves as a **draft** the operator reviews and publishes, and only published
        skills are surfaced — say you saved it rather than implying it is already in
        use."""
        store = ctx.deps.caps.get_optional(SkillStore)
        if store is None:
            return {"ok": False, "error": _UNAVAILABLE}
        try:
            skill = await store.create(
                ctx.deps.owner_id,
                name=name,
                description=description,
                body=body,
                source="agent",
            )
        except SkillValidationError as exc:
            raise ModelRetry(f"That skill was rejected ({exc.field}): {exc}") from None
        return {
            "ok": True,
            "name": skill.name,
            "status": "draft",
            "note": "Saved as a draft for the operator to review and publish.",
        }

    # Approval-gated (`AE-3`), statically rather than conditionally: unlike a document,
    # a skill has no "born in this conversation" case to exempt — `edit` resolves
    # `published_only`, so the only skills it can reach are ones the operator themselves
    # published, and a skill the agent created here is still a draft it cannot touch.
    # The stakes are why: a published skill's body is loaded and *followed* on every later
    # `skills_open`, in conversations that have nothing to do with this one, so untrusted
    # content the agent merely read this turn — a fetched page, an email — could otherwise
    # instruct it to poison a standing playbook permanently, with nothing shown to the
    # operator. Marked on the tool (rather than raising `ApprovalRequired` inside it) so
    # `tools/catalog._is_statically_gated` discovers it by inspection and it needs no
    # `gated_tools` declaration in the skills manifest.
    @toolset.tool(requires_approval=True)
    async def edit(
        ctx: RunContext[RunDeps],
        name: str,
        old_text: str,
        new_text: str,
        explanation: str,
    ) -> dict[str, object]:
        """Make a small, targeted change to a skill's instructions — refine a step, fix a
        detail, add a pitfall you just hit — without rewriting the whole thing.

        A published skill is followed in *future* conversations too, well beyond this
        one. ``explanation`` MUST say what you are changing and why.

        ``old_text`` must appear **exactly once** in the skill's text; include enough
        surrounding context to make it unique. To append rather than replace, use the last
        line of the skill as ``old_text`` and return it followed by your new text."""
        del explanation  # Read by the approval card from the call's arguments, not by this body.
        store = ctx.deps.caps.get_optional(SkillStore)
        if store is None:
            return {"ok": False, "error": _UNAVAILABLE}
        try:
            # published_only, exactly as `open` resolves it: a draft is the operator's
            # unreviewed work, so the agent must not be able to rewrite one — nor learn
            # it exists from a different error message.
            skill = await store.get_by_name(ctx.deps.owner_id, name, published_only=True)
        except NotFoundError:
            stored = await store.catalog(ctx.deps.owner_id)
            available = ", ".join(entry.name for entry in stored) or "none yet"
            raise ModelRetry(
                f"No published skill named {name!r}. Available skills: {available}."
            ) from None
        try:
            _ = await store.replace_span(ctx.deps.owner_id, skill.id, old_text, new_text)
        except SkillSpanError as exc:
            if exc.occurrences == 0:
                raise ModelRetry(
                    "old_text was not found in that skill. Quote its existing text exactly, "
                    + "including whitespace."
                ) from None
            raise ModelRetry(
                f"old_text matched {exc.occurrences} places in that skill. Include more "
                + "surrounding context so it identifies exactly one."
            ) from None
        return {"ok": True, "name": skill.name, "note": "Skill updated."}

    return toolset


def skill_catalog_block(entries: Sequence[SkillCatalogEntry]) -> str:
    """The skills line of the brief (`SKILL-2`): the platform's skills by name, then a count
    of the rest and the pinned ones. Entries arrive newest first, so a pin past the limit
    drops the oldest; both lists are sorted by name so an edit that keeps the set keeps the
    bytes."""
    if not entries:
        return ""
    platform = sorted(
        (entry for entry in entries if entry.source == "platform"), key=lambda e: e.name
    )
    saved = [entry for entry in entries if entry.source != "platform"]
    listed = (
        SKILL_PLATFORM.format(entries="\n".join(f"- {e.name}: {e.description}" for e in platform))
        if platform
        else ""
    )
    return SKILL_CATALOG.format(listed=listed, saved=_saved_block(saved))


def _saved_block(entries: Sequence[SkillCatalogEntry]) -> str:
    if not entries:
        return ""
    count = f"{len(entries)} saved skill{'' if len(entries) == 1 else 's'}"
    pinned = sorted(
        [entry for entry in entries if entry.pinned][:SKILL_PINNED_LIMIT],
        key=lambda entry: entry.name,
    )
    listed = (
        SKILL_PINNED.format(
            entries="\n".join(f"- {entry.name}: {entry.description}" for entry in pinned)
        )
        if pinned
        else ""
    )
    return SKILL_SAVED.format(count=count, pinned=listed)


async def skill_catalog_instructions(ctx: RunContext[RunDeps]) -> str:
    """Tell the agent which skills exist and how to open them (`SKILL-2`). A dynamic
    instruction, so it is always current and lives outside history."""
    return skill_catalog_block(await skill_catalog(ctx))
