import { expect, test } from "bun:test";
import { createStore } from "solid-js/store";
import type { RunEvent } from "~/lib/stream";
import fixture from "./__fixtures__/stream-parity.json";
import { toMessage } from "./data/messages";
import type { MessageDTO } from "./data/wire";
import type { AssistantBlock, ChatMessage } from "./model";
import { createFolder, type FoldState } from "./stream/fold";
import { createPatchById } from "./stream/patch";

/**
 * A turn reaches the transcript twice — folded from events while it runs, decoded from
 * the persisted message once the conversation is read back — and the two must draw the
 * same blocks in the same order, or the work log regroups itself the moment the run ends.
 *
 * Both halves come from one real turn, recorded by the backend's `test_stream_parity.py`,
 * which fails whenever either half changes shape until the fixture is regenerated. So a
 * change on either side of the wire lands here.
 */

function foldLive(events: RunEvent[]): AssistantBlock[] {
  const [messages, setMessages] = createStore<ChatMessage[]>([
    { id: "a1", role: "assistant", content: "", blocks: [], createdAt: "" },
  ]);
  const state: FoldState = {
    maxFoldedSeq: 0,
    foldTarget: null,
    tasksRevision: 0,
    planRevision: 0,
    activeRunId: "run-1",
    runKind: null,
    epoch: 0,
    replayThrough: 0,
  };
  const fold = createFolder({
    state,
    patchById: createPatchById(messages, setMessages),
    setMessages,
    setSnapshots: () => {},
    setTasks: () => {},
    setPlan: () => {},
    setPermission: () => {},
    setUsage: () => {},
    setStats: () => {},
    setErrored: () => {},
    setTitlePending: () => {},
  });
  for (const ev of events) fold("a1", ev);
  return messages[0].blocks ?? [];
}

/** What a reader sees of a block: its kind and what it says or which call it is.
 *  Ids are left out on purpose — live ones are client-minted, reloaded ones are not. */
function shape(blocks: AssistantBlock[]): string[] {
  return blocks.map((b) => {
    switch (b.kind) {
      case "thinking":
      case "text":
        return `${b.kind}: ${b.text}`;
      case "tool":
        return `tool: ${b.tool.id} ${b.tool.status}`;
      case "host_command":
        return `host_command: ${b.command.toolCallId}`;
      default:
        return b.kind;
    }
  });
}

test("a reload draws the turn in the order it streamed", () => {
  const live = shape(foldLive(fixture.events as RunEvent[]));
  const reloaded = shape(
    toMessage(fixture.message as unknown as MessageDTO).blocks ?? [],
  );
  // Guard against a fixture that stopped interleaving — parity over a flat turn would
  // pass for the very bug this exists to catch.
  expect(live.filter((s) => s.startsWith("thinking")).length).toBeGreaterThan(
    1,
  );
  expect(reloaded).toEqual(live);
});
