/**
 * A persisted turn, read as the transcript's own vocabulary.
 *
 * The cold conversation load's half of the pair — `toMessage` rebuilds an assistant
 * turn's blocks from the order the backend recorded them in (`segments`), the same order
 * the live stream handed them over in. Everything it reaches for (a host command, a
 * version chip, a citation) comes from the module that owns that shape, so the two
 * readers of a payload cannot disagree about what it means.
 *
 * Pure: nothing here touches a store, a resource or the network. Ordering, deduping and
 * placement beyond a single turn belong to whoever is folding.
 */

import { asCompactionReason } from "../compactionReason";
import {
  NARRATION_ARG,
  describeToolArgs,
  describeToolResult,
  toolNarration,
} from "../toolSummary";
import type {
  AssistantBlock,
  ChatMessage,
  Citation,
  ToolImage,
  ToolInvocation,
} from "../model";
import { isTerminalTool, toolEntry } from "../toolPresentation";
import { placeToolCall } from "../toolTree";
import type { MessageDTO, ToolCallDTO, ToolImageDTO } from "./wire";
import { citationsFromToolResult } from "./citations";
import { commandBoundary, toHostCommand } from "./hostCommands";
import { toVersionChipBlock } from "./viewSnapshots";

/** Every argument as `k=v`, for the expanded card.
 *
 *  **Except the narration**, which the collapsed row already leads with. It is the
 *  one argument written for the operator rather than for the tool, so printing it
 *  again in the raw dump would put the same sentence on screen twice — and the dump
 *  exists to show what the *tool* received, which by then no longer includes it. */
export function formatArgs(args: Record<string, unknown>): string {
  return Object.entries(args)
    .filter(([k]) => k !== NARRATION_ARG)
    .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join(", ");
}

export function stringifyResult(result: unknown): string | undefined {
  if (result == null) return undefined;
  return typeof result === "string" ? result : JSON.stringify(result, null, 2);
}

/** The wire's snake_case image list as the model's, or undefined when a call returned
 *  none — the same mapping for the live event and the cold DTO, since a screenshot has
 *  to look identical whether the operator watched it happen or reloaded into it. */
export function toolImages(
  images: ToolImageDTO[] | undefined,
): ToolImage[] | undefined {
  if (!images?.length) return undefined;
  return images.map((i) => ({ mediaType: i.media_type, data: i.data }));
}

/** A call as its arguments describe it — everything a `tool.started` frame and a
 *  persisted row agree on before either says how the call went. One reading for both,
 *  so a live row and a reloaded one cannot word the same call differently. */
export function startedTool(
  id: string,
  name: string,
  args: Record<string, unknown>,
): ToolInvocation {
  const scriptKey = toolEntry(name).script;
  const raw = scriptKey ? args[scriptKey] : undefined;
  const script = typeof raw === "string" ? raw : undefined;
  const shown =
    script === undefined
      ? args
      : Object.fromEntries(
          Object.entries(args).filter(([k]) => k !== scriptKey),
        );
  return {
    id,
    name,
    args: formatArgs(shown),
    detail: describeToolArgs(name, args),
    narration: toolNarration(args),
    script,
    status: "running",
  };
}

export function toTool(dto: ToolCallDTO): ToolInvocation {
  return {
    ...startedTool(dto.id, dto.name, dto.args),
    status: dto.status,
    // Only a call that succeeded has an outcome to report; a failure's story is
    // its error, which the card shows in full.
    outcome:
      dto.status === "ok"
        ? describeToolResult(dto.name, dto.result)
        : undefined,
    result: stringifyResult(dto.result),
    error: dto.error ?? undefined,
    images: toolImages(dto.images),
    // A backgrounded command is still a command run on the operator's machine, and it
    // renders as an ordinary card rather than as a terminal — so this is the only place
    // its declaration and fence would otherwise be dropped.
    boundary: commandBoundary(dto.result),
  };
}

/** Items bucketed by key, each bucket in input order. */
function groupBy<T, K>(items: T[], key: (item: T) => K): Map<K, T[]> {
  const groups = new Map<K, T[]>();
  for (const item of items) {
    const k = key(item);
    const bucket = groups.get(k);
    if (bucket) bucket.push(item);
    else groups.set(k, [item]);
  }
  return groups;
}

export function toMessage(dto: MessageDTO): ChatMessage {
  const base: ChatMessage = {
    id: dto.id,
    role: dto.role,
    author: dto.author ?? undefined,
    content: dto.content,
    createdAt: dto.created_at ?? new Date().toISOString(),
    versionIndex: dto.version_index,
    versionCount: dto.version_count,
    pinned: dto.pinned,
    attachmentIds: dto.attachment_ids,
    fileRefs: dto.file_refs,
    foldedMessages: dto.messages_compacted ?? undefined,
    tokensBefore: dto.tokens_before ?? undefined,
    tokensAfter: dto.tokens_after ?? undefined,
    compactionReason: asCompactionReason(dto.compaction_reason),
    // Length, not `??`: the backend defaults this to `[]` on *every* row, and an empty
    // array is not nullish — so `?? undefined` would hang a useless empty list off every
    // user and assistant turn. Absent-not-empty, the same rule the fields above follow.
    summarySections: dto.sections?.length ? dto.sections : undefined,
  };
  if (dto.role !== "assistant") return base;
  const blocks: AssistantBlock[] = [];
  const citations: Citation[] = [];
  const toolBlockId = (callId: string) => `${dto.id}-${callId}`;
  // Each version chip keyed by the `show` that minted it, so it can follow that call as
  // it did live. Placing a chip removes it, which is what lets the sweep at the end
  // place every chip whose call is unknown rather than dropping it.
  const versionsByCall = groupBy(
    dto.versions ?? [],
    (v) => v.tool_call_id ?? "",
  );
  const placeVersions = (callId: string) => {
    for (const v of versionsByCall.get(callId) ?? [])
      blocks.push(
        toVersionChipBlock(dto.id, {
          snapshotId: v.snapshot_id,
          title: v.title ?? undefined,
          previewKind: v.preview_kind,
        }),
      );
    versionsByCall.delete(callId);
  };
  // A script's own calls are not segments of the turn — they nest on its card, and are
  // placed with it so that anything they left behind (a chip) lands where it did live.
  const childrenOf = groupBy(
    dto.tools.filter((t) => t.parent_tool_call_id),
    (t) => t.parent_tool_call_id!,
  );
  const placed = new Set<string>();
  const placeCall = (t: ToolCallDTO): void => {
    if (placed.has(t.id)) return;
    placed.add(t.id);
    // A call a script made nests on the script's card, by the same rule the live fold
    // places it with — including a terminal command, which reads as a compact row there.
    if (t.parent_tool_call_id)
      placeToolCall(blocks, toolBlockId, toTool(t), t.parent_tool_call_id);
    // The same question the live fold asks, off the same table — so a reload cannot
    // turn a terminal back into a generic tool card.
    else if (isTerminalTool(t.name))
      blocks.push({
        kind: "host_command",
        id: toolBlockId(t.id),
        command: toHostCommand(t),
      });
    else placeToolCall(blocks, toolBlockId, toTool(t));
    // A settled `ask_user` carries what it asked and what it was told. Rebuilt as the
    // same answered question block the live `question.answered` fold produces, so a
    // reload renders the exchange rather than leaving it as prose inside a tool row.
    // Placed after its own call for the same reason the live one sits on the turn it
    // belongs to.
    if (t.answers?.length)
      blocks.push({
        kind: "question",
        id: `${dto.id}-${t.id}-answers`,
        question: {
          toolCallId: t.id,
          questions: [],
          answers: t.answers.map((a) => ({
            question: a.question,
            selections: a.selections ?? [],
            text: a.text ?? undefined,
          })),
        },
      });
    for (const c of citationsFromToolResult(t.name, t.result))
      if (!citations.some((existing) => existing.url === c.url))
        citations.push(c);
    placeVersions(t.id);
    for (const child of childrenOf.get(t.id) ?? []) placeCall(child);
  };
  // The backend's record of emission order, replayed as is, so a reload draws the
  // think → call → text → … sequence the operator watched stream rather than one lane
  // per kind. `streamParity.test.ts` holds this against the live fold.
  const byId = new Map(dto.tools.map((t) => [t.id, t] as const));
  (dto.segments ?? []).forEach((s, i) => {
    if (s.kind === "tool") {
      const t = byId.get(s.tool_call_id);
      if (t) placeCall(t);
    } else if (s.text)
      blocks.push({
        kind: s.kind,
        id: `${dto.id}-${s.kind}-${i}`,
        text: s.text,
      });
  });
  // Whatever no segment reached — a call whose script is not on this turn, a chip whose
  // call is unknown — still renders, at the end, rather than vanishing.
  for (const t of dto.tools) placeCall(t);
  for (const callId of [...versionsByCall.keys()]) placeVersions(callId);
  // The answer lives in the text block(s); keep `content` empty for assistant
  // turns so it isn't a second, divergent copy of the same text.
  return {
    ...base,
    content: "",
    blocks,
    citations: citations.length ? citations : undefined,
    blocked: dto.blocked_reason != null,
    blockedDetail: dto.blocked_reason ?? undefined,
    model: dto.model ?? undefined,
  };
}
