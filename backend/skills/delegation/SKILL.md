---
name: delegation
description: Decide when to hand work to other agents, brief them, and check what comes back. Use before spawning agents for anything larger than one self-contained job.
---

# Delegation

You stay in charge. Agents you spawn work for you and report back to you; the operator hears from you, not from them.

## 1. Decide whether to delegate

Delegate when the work splits into parts that can go on at the same time, or when a part needs a lot of reading or trial and error whose detail you do not need to keep. Do it yourself when it takes a couple of tool calls, when every step depends on the last, or when the brief would be longer than the work.

Each agent costs a run, a share of your endpoint and a report to read. Spawn as many as the work actually splits into, not more.

## 2. Pick the agent

The `agents_spawn` description lists the agents you can spawn and what each is for. When none fits and the work will recur, draft one with the `build-an-agent` skill rather than stretching a general one. Prefer a read-only agent for anything that does not need to write: spawning one that can write asks the operator first.

## 3. Write a brief that stands alone

The agent never sees this conversation. The `task` must say:

- what to do, and why it matters, in a sentence or two;
- where to start: the files, URLs, names or prior findings it would otherwise rediscover;
- what is out of scope, especially work another agent already has;
- what a finished answer looks like: its shape, how long it should be, and what evidence to include.

Name each agent for its job (`auth-route-reviewer`), so the tree reads as a plan.

Use `isolate` when the agent will edit files while you keep editing yours. Its copy is merged back when it finishes.

## 4. Let them work

Issue every spawn together in one step, then carry on or end your turn. Reports arrive on their own: never poll, and never wait in a loop.

- When what you need from an agent changes, `agents_message` it with the amended brief rather than spawning a second one.
- Agents in one tree can message each other: tell an agent the handle of a sibling whose result it needs. Any agent, read-only ones included, can message `parent` or `main` at any time to report something early.
- A message across or down the tree to an agent that can write asks the operator first, as spawning one does. Messages up the tree and to read-only agents never ask.
- Stop an agent with `agents_stop` once its work is moot. It reports what it got through.

## 5. Check before you relay

Before concluding, call `agents_list` and treat anything still working as unread.

Read each report as a claim, not a fact. Check what you can cheaply check: open the file it says it changed, run the test it says passes, follow the link it cites. Where two reports disagree, resolve it or say so. Never pass a report to the operator as your own finding without reading it, and say which parts you verified.
