/**
 * The small mutations the fold performs on one assistant message.
 *
 * Every one of these takes a message and edits it in place — they are meant to be called
 * from inside a Solid `produce`, where the draft is already a mutable proxy. Keeping them
 * out of the fold's switch is what stops that switch from being an argument about *how* a
 * block is found or created while it is trying to say *which* block an event belongs to.
 *
 * The identity counter lives here too, because the same rule governs it: a live block and
 * a live message both need an id that no persisted row will ever collide with, and one
 * counter is the only way to be sure of that.
 */

import { produce, type SetStoreFunction } from "solid-js/store";
import { isAnswered } from "../model";
import type {
  ChatMessage,
  HostCommand,
  HostCommandBlock,
  ReviewBlock,
  ToolInvocation,
} from "../model";
import { findToolCall } from "../toolTree";

let counter = 0;

/** Edit one message of a transcript in place, found by id. */
export type PatchById = (id: string, fn: (m: ChatMessage) => void) => void;

/**
 * Bind a `patchById` to one transcript store.
 *
 * A factory rather than a plain function because of the hint it keeps. Every
 * `answer.delta` and `thinking.delta` patches a message by id — once per *token* — and
 * resolving that id with a `findIndex` walks the array through Solid's store proxy,
 * materializing a wrapper per element on the way. The id is nearly always the bubble the
 * run is folding into, and it is nearly always in the same place it was a token ago, so
 * the last hit is remembered and checked first: the common case costs one comparison,
 * and anything that moves the message (an injected boundary, a withdraw, a reseat, the
 * server's ids being adopted) simply misses the hint and falls back to the scan.
 *
 * The fold and the controller both need this, over the same store — two copies of it
 * were two places to remember the scan was the expensive part.
 */
export function createPatchById(
  messages: ChatMessage[],
  setMessages: SetStoreFunction<ChatMessage[]>,
): PatchById {
  let hint = -1;
  return (id, fn) => {
    const i =
      hint >= 0 && hint < messages.length && messages[hint].id === id
        ? hint
        : messages.findIndex((m) => m.id === id);
    if (i < 0) return;
    hint = i;
    setMessages(produce((m) => fn(m[i])));
  };
}

/** A client-minted id for something the stream produced but the backend hasn't named
 *  yet — a live block, or an assistant bubble a `message.injected` boundary opened. */
export const nextId = (prefix: string) => `${prefix}-live-${++counter}`;

/** Append a streamed delta onto the trailing block of `kind`, starting a new
 *  block whenever the kind changes. This is what turns the flat delta stream
 *  into an ordered, interleaved sequence — and what gives a turn *multiple*
 *  thinking blocks (each resumption after a tool/text starts a fresh one). */
export function appendDelta(
  m: ChatMessage,
  kind: "thinking" | "text",
  text: string,
): void {
  const blocks = m.blocks ?? (m.blocks = []);
  const last = blocks[blocks.length - 1];
  if (last && last.kind === kind) last.text += text;
  else blocks.push({ kind, id: nextId(kind), text });
}

/** Take the last `chars` of streamed `kind` text back off the trailing text and
 *  reasoning blocks, dropping a block it empties. */
export function retractDelta(
  m: ChatMessage,
  kind: "thinking" | "text",
  chars: number,
): void {
  const blocks = m.blocks ?? [];
  let left = chars;
  for (let i = blocks.length - 1; i >= 0 && left > 0; i--) {
    const block = blocks[i];
    if (block.kind !== "text" && block.kind !== "thinking") break;
    if (block.kind !== kind) continue;
    const cut = Math.min(left, block.text.length);
    block.text = block.text.slice(0, block.text.length - cut);
    left -= cut;
    if (!block.text) blocks.splice(i, 1);
  }
}

/** The generic tool call for one id — a rail block's, or one nested on a script's card,
 *  since a script's calls report progress and settle by id like any other. */
export function findTool(
  m: ChatMessage,
  toolCallId: string,
): ToolInvocation | undefined {
  return findToolCall(m.blocks, toolCallId);
}

/** Remove the top-level block of a call still in `preparing` — all of them, or one id. */
export function dropPreparing(m: ChatMessage, toolCallId?: string): void {
  if (!m.blocks) return;
  m.blocks = m.blocks.filter(
    (b) =>
      !(
        b.kind === "tool" &&
        b.tool.status === "preparing" &&
        (toolCallId === undefined || b.tool.id === toolCallId)
      ),
  );
}

/** The terminal block for one call, keyed by tool_call_id.
 *
 *  `upsertHost` finds the same block to write to it; this is the read, for a frame that
 *  must **not** open one that isn't there. A progress delta belongs to whatever block
 *  already exists for the call, and which kind that is — a terminal or a generic tool
 *  card — is what decides whether the delta appends or replaces. */
export function findHost(
  m: ChatMessage,
  toolCallId: string,
): HostCommandBlock | undefined {
  return m.blocks?.find(
    (b): b is HostCommandBlock =>
      b.kind === "host_command" && b.command.toolCallId === toolCallId,
  );
}

/** Apply the operator's verdict on one parked call to `m`: a pending terminal starts
 *  running or closes denied, an approval card retires. False when `m` holds neither.
 *  Shared by the optimistic settle and the `approval.decided` fold, which must agree. */
export function settleDecision(
  m: ChatMessage,
  toolCallId: string,
  approved: boolean,
): boolean {
  const host = findHost(m, toolCallId);
  if (host) {
    if (host.command.phase === "pending")
      host.command.phase = approved ? "running" : "denied";
    return true;
  }
  if (
    !m.blocks?.some(
      (b) => b.kind === "approval" && b.approval.toolCallId === toolCallId,
    )
  )
    return false;
  clearPark(m, toolCallId);
  return true;
}

/** The review row for one call, keyed by tool_call_id — `review.started` opens it and
 *  `review.completed` fills in the verdict on the same block, so the row the operator
 *  saw appear is the row that ends up carrying the answer. */
export function findReview(
  m: ChatMessage,
  toolCallId: string,
): ReviewBlock | undefined {
  return m.blocks?.find(
    (b): b is ReviewBlock =>
      b.kind === "review" && b.review.toolCallId === toolCallId,
  );
}

/** Drop the prompt one call parked on — `approval.required` / `question.asked` open it,
 *  the call's own result retires it. A park block is a prompt, not a record (the tool
 *  block is the record), and a replay carries the result but not the click that answered
 *  it. Keyed per call: a turn can park on two and settle them separately. */
export function clearPark(m: ChatMessage, toolCallId: string): void {
  if (!m.blocks) return;
  m.blocks = m.blocks.filter(
    (b) =>
      !(
        (b.kind === "approval" && b.approval.toolCallId === toolCallId) ||
        // A question that carries its answers is no longer a park but is still a row in
        // the transcript — what was asked and what was said, rendered as an exchange.
        // Only one that has none is dropped, which is also the degrade path: an answer
        // that never arrived leaves the block retired exactly as it always was, rather
        // than stranding an unanswerable question on a settled turn.
        (b.kind === "question" &&
          b.question.toolCallId === toolCallId &&
          !isAnswered(b.question))
      ),
  );
}

/** Upsert a terminal *block*, keyed by tool_call_id. A terminal call's
 *  `tool.started`, `approval.required`, and `tool.completed` events all land
 *  here, each filling in the part it carries onto the same block.
 *
 *  `name` rides along because more than one tool renders as a terminal and a
 *  conversation grant is recorded against a tool name — the card cannot assume which
 *  one it is holding. A new block lands at `at`, else at the end. */
export function upsertHost(
  m: ChatMessage,
  toolCallId: string,
  name: string,
  patch: Partial<HostCommand>,
  at?: number,
): void {
  const existing = findHost(m, toolCallId);
  if (existing) {
    // A denied terminal is settled by the operator's own decision, and nothing the
    // tool reports afterwards may un-settle it. A denial still arrives as a
    // `tool.completed` carrying the refusal the model was handed — which for a tool
    // whose ordinary result is *also* a plain string would otherwise be read as
    // output and repaint a command that never ran as a green OK.
    if (existing.command.phase === "denied") return;
    Object.assign(existing.command, patch);
  } else {
    const blocks = m.blocks ?? (m.blocks = []);
    blocks.splice(at ?? blocks.length, 0, {
      kind: "host_command",
      id: `host-${toolCallId}`,
      command: { toolCallId, name, command: "", phase: "pending", ...patch },
    });
  }
}
