---
name: delegation
description: Decide when to hand work to other agents, brief them, and check what comes back. Use before spawning agents for anything larger than one self-contained job.
---

# Delegation

You stay in charge. Agents you spawn work for you and report back to you; the operator hears from you, not from them.

## 1. Decide whether to delegate

Delegate when the work splits into parts that can go on at the same time, or when a part needs a lot of reading or trial and error whose detail you do not need to keep. Do it yourself when it takes a couple of tool calls, when every step depends on the last, or when the brief would be longer than the work.

Long churn is the clearest case. A refactor across many files, a long test-and-fix loop, or a sweep through many files or results would fill your context with detail you only need the conclusion of. Give it to one agent: its churn stays in its context, and only its report enters yours. An agent is stopped when it stalls, never for running long, so a task that runs a long while is fine to hand over.

When the operator asks for an agent ("have a test runner run the suite"), how the work gets done is a choice they made, so spawn one; if you expect it to fail, its report will show them why.

Each agent costs a run, a share of your endpoint and a report to read. Spawn as many as the work actually splits into, not more.

## 2. Pick the agent

The `agents_spawn` description lists the agents you can spawn and what each is for. When none fits and the work will recur, draft one with the `build-an-agent` skill rather than stretching a general one. Prefer a read-only agent for anything that does not need to write: an agent that can write makes changes at your permission level, and each one is gated like yours.

## 3. Write a brief that stands alone

The agent never sees this conversation. The `task` must say:

- what to do, and why it matters, in a sentence or two;
- where to start: the files, URLs, names or prior findings it would otherwise rediscover;
- what is out of scope, especially work another agent already has;
- what a finished answer looks like: its shape, how long it should be, and what evidence to include.

Name each agent for its job (`auth-route-reviewer`), so the tree reads as a plan.

Use `worktree` when the agent will edit a project while you keep editing it yourself. It works on its own branch and worktree, and its work waits there for you.

## 4. Let them work

Issue every spawn together in one step, then carry on or end your turn. Reports arrive on their own and wake you, so polling or waiting in a loop spends calls without learning anything.

- When what you need from an agent changes, `agents_message` it with the amended brief rather than spawning a second one.
- Agents in one tree can message each other: tell an agent the handle of a sibling whose result it needs. Any agent, read-only ones included, can message `parent` or `main` at any time to pass something on early. A question an agent needs answered goes in its `blocked` report instead.
- Spawning and messaging never ask the operator. An agent you spawn may do what you may, at your permission level.
- Stop an agent with `agents_stop` once its work is moot. It reports what it got through.

## 5. Check before you relay

Before concluding, call `agents_list` and treat anything still working as unread.

Read each report as a claim, not a fact. Check what you can cheaply check: open the file it says it changed, run the test it says passes, follow the link it cites. Where two reports disagree, resolve it or say so. A report you pass on unread is the agent's claim in your voice, so say which parts you verified, and mark as unchecked anything the agent itself says it did not check.

An agent spawned with `worktree` reports the branch and the path its work is at (`~/worktrees/<project>-<name>`). Review the change there, run its checks, and fix what needs fixing in that worktree. Then `agents_merge` it to land it where you work, or `agents_discard` it. A merge that clashes changes nothing and names the files: bring its worktree in line with yours and merge again.
