---
name: build-an-agent
description: Draft a new agent with its own standing brief, tools and limits for the operator to publish. Use when recurring work deserves a specialist rather than a one-off brief.
---

# Build an agent

An agent is a standing brief plus limits: what it is for, what it may touch, and how far it may go. Write one when the same kind of delegated work keeps coming back. For a one-off job, spawn an existing agent with a good brief instead.

## 1. Check what exists

Search the catalog first (`platform_search`, sources `agents`). Improving or reusing an agent beats adding a near-duplicate the model then has to choose between.

## 2. Write it with `agents_write`

- `name`: lowercase, hyphens, named for the role (`changelog-writer`).
- `description`: one or two sentences saying what it does and when to pick it. It is all a delegating agent reads when choosing, so lead with the trigger.
- `instructions`: the brief it runs under, in the second person. Say how to approach the work, what good output looks like, what to report back, and the mistakes to avoid. Leave out anything every task will state anyway.
- `tools`: the tools or categories it needs (`files`, `shell`, `web_search`). Fewer tools make a sharper agent. Leave it unset only for a generalist.
- `deny_tools`: tools to withhold even though a category grants them.
- `permission_ceiling`: the most it may do without asking. Use read-only for anything that only investigates.
- `workspace`: `shared` works in the spawner's files; `isolated` works on a branch and worktree of its own, which the spawner reviews and merges; `own` has a scratch of its own.
- `can_delegate`: only when the role genuinely fans out further.

## 3. Hand it over

What you write is a draft, and drafts run nowhere. Tell the operator what you drafted and why, then call `agents_publish` to ask them to publish it, or leave it for them to review in the Agents area. Changing an agent that is already published also asks first.

## 4. Verify

Once it is published, spawn it on a small real task and read its report. If it went wrong, fix the brief, not just the task.
