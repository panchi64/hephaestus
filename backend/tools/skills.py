"""Skill tools — the agent opens a playbook and works from it (`SKILL-2`, `SKILL-3`).

Skills follow the **Agent Skills** standard's progressive disclosure, and these tools are
levels two and three of it:

1. The turn's instructions say how many skills are published and list the pinned ones;
   the rest are found by name and description through ``platform_search``.
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

from collections.abc import Sequence

from pydantic_ai import FunctionToolset, ModelRetry, RunContext

from core.exceptions import NotFoundError, SkillSpanError, SkillValidationError
from prompts.agent import SKILL_CATALOG, SKILL_PINNED, SKILL_PINNED_LIMIT
from services.projects.worktree import WorktreeBusyError
from services.sandbox import SandboxError
from services.skills import (
    BUNDLE_MAX_BYTES,
    SKILL_FILE,
    SkillCatalogEntry,
    SkillStore,
    render_skill_md,
)
from services.skills.store import SkillView
from services.workspace import RunWorkspace

from .deps import RunDeps
from .workspace import run_workspace

#: Where a staged bundle lands inside the sandbox working directory (mirrors
#: ``attachments/`` from the attachments tool).
_STAGE_DIR = "skills"
_UNAVAILABLE = "Skills are unavailable right now."


async def _published_names(store: SkillStore, owner_id: str) -> str:
    entries = await store.catalog(owner_id)
    return ", ".join(entry.name for entry in entries) or "none yet"


async def _stage(
    workspace: RunWorkspace, skill: SkillView, files: list[tuple[str, bytes]]
) -> list[str]:
    """Write the bundle into the run's workspace, skipping any file already there
    byte-for-byte so re-opening a skill in a warm session is a no-op rather than a
    rewrite. Returns the absolute paths the model should use — which is why they come
    from the workspace rather than a literal ``/work``: a worktree run's files are on the
    host, and telling the model about a sandbox path it cannot reach is worse than not
    staging at all."""
    root = f"{workspace.stage_prefix}{_STAGE_DIR}/{skill.name}"
    staged: list[str] = []
    for relpath, blob in [(SKILL_FILE, render_skill_md(skill.parsed()).encode())] + files:
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
    async def open(ctx: RunContext[RunDeps], name: str) -> dict:
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
        store = ctx.deps.caps.get_optional(SkillStore)
        if store is None:
            return {"ok": False, "error": _UNAVAILABLE}
        try:
            skill = await store.get_by_name(ctx.deps.owner_id, name, published_only=True)
        except NotFoundError:
            available = await _published_names(store, ctx.deps.owner_id)
            raise ModelRetry(
                f"No published skill named {name!r}. Available skills: {available}."
            ) from None

        result: dict = {
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

        files = await store.file_contents(ctx.deps.owner_id, skill.id)
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
    async def create(ctx: RunContext[RunDeps], name: str, description: str, body: str) -> dict:
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
    ) -> dict:
        """Make a small, targeted change to a skill's instructions — refine a step, fix a
        detail, add a pitfall you just hit — without rewriting the whole thing.

        A published skill is followed in *future* conversations too, well beyond this
        one. ``explanation`` MUST say what you are changing and why.

        ``old_text`` must appear **exactly once** in the skill's text; include enough
        surrounding context to make it unique. To append rather than replace, use the last
        line of the skill as ``old_text`` and return it followed by your new text."""
        store = ctx.deps.caps.get_optional(SkillStore)
        if store is None:
            return {"ok": False, "error": _UNAVAILABLE}
        try:
            # published_only, exactly as `open` resolves it: a draft is the operator's
            # unreviewed work, so the agent must not be able to rewrite one — nor learn
            # it exists from a different error message.
            skill = await store.get_by_name(ctx.deps.owner_id, name, published_only=True)
        except NotFoundError:
            available = await _published_names(store, ctx.deps.owner_id)
            raise ModelRetry(
                f"No published skill named {name!r}. Available skills: {available}."
            ) from None
        try:
            await store.replace_span(ctx.deps.owner_id, skill.id, old_text, new_text)
        except SkillSpanError as exc:
            if exc.occurrences == 0:
                raise ModelRetry(
                    "old_text was not found in that skill. Quote its existing text exactly, "
                    "including whitespace."
                ) from None
            raise ModelRetry(
                f"old_text matched {exc.occurrences} places in that skill. Include more "
                "surrounding context so it identifies exactly one."
            ) from None
        return {"ok": True, "name": skill.name, "note": "Skill updated."}

    return toolset


def _skill_catalog_block(entries: Sequence[SkillCatalogEntry]) -> str:
    """The catalog line for the published skills (`SKILL-2`): a count, and the pinned ones
    by name. Entries arrive newest first, so a pin past the limit drops the oldest; the
    survivors are sorted by name so an edit that keeps the set keeps the bytes."""
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
    return SKILL_CATALOG.format(count=count, pinned=listed)


async def skill_catalog_instructions(ctx: RunContext[RunDeps]) -> str:
    """Tell the agent the operator's published skills exist and how to find them
    (`SKILL-2`). A dynamic instruction, so it is always current and lives outside history.
    Empty when there's no skill store or nothing is published."""
    store = ctx.deps.caps.get_optional(SkillStore)
    if store is None:
        return ""
    return _skill_catalog_block(await store.catalog(ctx.deps.owner_id))
