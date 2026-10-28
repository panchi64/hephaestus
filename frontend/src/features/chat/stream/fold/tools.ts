/**
 * The tool family: a call's life (started, progress, completed, failed), the review the
 * chassis runs on it at Auto, and the two ways a run parks on the operator — an approval
 * and a question.
 */

import { produce } from "solid-js/store";
import type { RunEvent } from "~/lib/stream";
import { approvalFromEvent, explanationOf } from "../../approvalEvent";
import {
  commandBoundary,
  commandReason,
  plainShellOutcome,
  toTerminalOutcome,
} from "../../data/hostCommands";
import {
  preparingTool,
  startedTool,
  stringifyResult,
  toContentParts,
  toolImages,
} from "../../data/messages";
import type { HostCommand } from "../../model";
import { isTerminalTool } from "../../toolPresentation";
import { describeToolResult } from "../../toolSummary";
import { placeToolCall } from "../../toolTree";
import {
  clearPark,
  dropPreparing,
  findHost,
  findReview,
  findTool,
  settleDecision,
  upsertHost,
} from "../patch";
import type { FoldContext } from "./types";

/** Fold one tool, review or park event; false when the event is not in this family. */
export function foldTools(ev: RunEvent, c: FoldContext): boolean {
  const { deps, assistantId } = c;
  const { patchById, setMessages } = deps;
  switch (ev.type) {
    case "tool.started":
      // Terminal tools come from the table (`ToolEntry.terminal`). tool.started fires
      // before approval.required, so this seeds the pending terminal. A script's call
      // nests on its card as a compact row, never a terminal.
      if (!ev.parent_tool_call_id && isTerminalTool(ev.name)) {
        patchById(assistantId, (m) => {
          const at = m.blocks?.findIndex(
            (b) =>
              b.kind === "tool" &&
              b.tool.status === "preparing" &&
              b.tool.id === ev.tool_call_id,
          );
          dropPreparing(m, ev.tool_call_id);
          upsertHost(
            m,
            ev.tool_call_id,
            ev.name,
            {
              command:
                typeof ev.args.command === "string" ? ev.args.command : "",
              explanation: commandReason(ev.args),
            },
            at === undefined || at < 0 ? undefined : at,
          );
        });
        return true;
      }
      patchById(assistantId, (m) => {
        placeToolCall(
          m.blocks ?? (m.blocks = []),
          (id) => `tool-${id}`,
          startedTool(ev.tool_call_id, ev.name, ev.args),
          ev.parent_tool_call_id,
        );
        const t = findTool(m, ev.tool_call_id);
        if (t) t.argsChars = undefined;
      });
      return true;
    case "tool.preparing":
      patchById(assistantId, (m) => {
        if (ev.replaces && !findTool(m, ev.tool_call_id)) {
          const old = m.blocks?.find(
            (b) =>
              b.kind === "tool" &&
              b.tool.status === "preparing" &&
              b.tool.id === ev.replaces,
          );
          if (old?.kind === "tool") {
            old.tool.id = ev.tool_call_id;
            old.id = `tool-${ev.tool_call_id}`;
          }
        }
        const t = findTool(m, ev.tool_call_id);
        if (!t) {
          placeToolCall(
            m.blocks ?? (m.blocks = []),
            (id) => `tool-${id}`,
            preparingTool(ev.tool_call_id, ev.name, ev.args_chars),
            null,
          );
        } else if (t.status === "preparing") t.argsChars = ev.args_chars;
      });
      return true;
    case "tool.progress":
      // The existing block decides the reading: a terminal's frames are deltas and
      // APPEND, any other tool's frame is a status note and REPLACES. The event carries
      // no tool name, and the block already encodes the table's answer.
      patchById(assistantId, (m) => {
        const host = findHost(m, ev.tool_call_id);
        if (host) {
          if (ev.partial)
            host.command.streamed = (host.command.streamed ?? "") + ev.partial;
          // The settled result carries the authoritative figure and overwrites it.
          if (ev.elapsed_s != null)
            host.command.elapsedMs = Math.round(ev.elapsed_s * 1000);
          return;
        }
        const t = findTool(m, ev.tool_call_id);
        if (t) {
          t.progress = ev.partial ?? undefined;
          if (ev.elapsed_s != null)
            t.elapsedMs = Math.round(ev.elapsed_s * 1000);
        }
      });
      return true;
    case "tool.completed": {
      // Retire the prompt the call parked on so a replay doesn't re-ask it.
      patchById(assistantId, (m) => clearPark(m, ev.tool_call_id));
      if (!ev.parent_tool_call_id && isTerminalTool(ev.name)) {
        // The result keeps the streams apart, so the concatenated streamed text is
        // dropped in the same patch.
        const outcome =
          toTerminalOutcome(ev.result) ?? plainShellOutcome(ev.name, ev.result);
        // No outcome plus a string means a guard refused the call and it never ran; the
        // cold read projects the same string the same way.
        const patch: Partial<HostCommand> | null =
          outcome ??
          (typeof ev.result === "string" && ev.result
            ? { phase: "denied", error: ev.result }
            : null);
        if (patch)
          patchById(assistantId, (m) =>
            upsertHost(m, ev.tool_call_id, ev.name, {
              ...patch,
              streamed: undefined,
            }),
          );
        return true;
      }
      patchById(assistantId, (m) => {
        const t = findTool(m, ev.tool_call_id);
        if (t) {
          t.status = "ok";
          t.result = stringifyResult(ev.result);
          t.resultParts = toContentParts(ev.result_parts);
          t.outcome = describeToolResult(ev.name, ev.result);
          t.progress = undefined; // the run is over — drop the spin-up note
          t.images = toolImages(ev.images);
          // Same read as the cold mapper, so a reload keeps the declaration and fence.
          t.boundary = commandBoundary(ev.result);
        }
      });
      return true;
    }
    case "tool.failed":
      // A failure settles the call too.
      patchById(assistantId, (m) => clearPark(m, ev.tool_call_id));
      if (!ev.parent_tool_call_id && isTerminalTool(ev.name)) {
        patchById(assistantId, (m) =>
          upsertHost(m, ev.tool_call_id, ev.name, {
            phase: "error",
            error: ev.error,
          }),
        );
        return true;
      }
      patchById(assistantId, (m) => {
        const t = findTool(m, ev.tool_call_id);
        if (t) {
          t.status = "error";
          t.error = ev.error;
          t.platform = ev.platform || undefined;
          t.progress = undefined; // the run is over — drop the spin-up note
        }
      });
      return true;
    case "review.started":
      // Opens on start, not on the verdict, so a model-call review reads as in flight
      // and lands ahead of the tool row it judges.
      patchById(assistantId, (m) => {
        if (findReview(m, ev.tool_call_id)) return;
        (m.blocks ?? (m.blocks = [])).push({
          kind: "review",
          id: `review-${ev.tool_call_id}`,
          review: {
            toolCallId: ev.tool_call_id,
            name: ev.name,
            summary: ev.summary,
            // Undefined rather than null, so the card renders each only when present.
            detail: ev.detail ?? undefined,
            reach: ev.reach ?? undefined,
            network: ev.network ?? undefined,
          },
        });
      });
      return true;
    case "review.completed":
      patchById(assistantId, (m) => {
        const b = findReview(m, ev.tool_call_id);
        if (!b) return;
        b.review.decision = ev.decision;
        b.review.stage = ev.stage;
        b.review.reason = ev.reason;
        // `tier` is the structural ground that cleared the call; `fenced` is whether
        // this host could hold it there.
        b.review.tier = ev.tier ?? undefined;
        b.review.fenced = ev.fenced;
        // Null on the wire means the model stage never ran.
        b.review.risk = ev.risk ?? undefined;
        b.review.authorization = ev.authorization ?? undefined;
        b.review.correctness = ev.correctness ?? undefined;
        b.review.scores = ev.scores ?? undefined;
        b.review.setup = ev.setup ?? undefined;
      });
      return true;
    case "approval.required": {
      // `args` is typed as present but arrives as untrusted JSON; default it once here.
      const args: Record<string, unknown> = ev.args ?? {};
      // At Auto the chassis reviewed the call first; its verdict rides onto whichever
      // card asks (terminal or approval) so e.g. `too_destructive` shows on the prompt.
      if (isTerminalTool(ev.name)) {
        patchById(assistantId, (m) => {
          const review = findReview(m, ev.tool_call_id)?.review;
          upsertHost(m, ev.tool_call_id, ev.name, {
            command: typeof args.command === "string" ? args.command : "",
            explanation: explanationOf(ev),
            guard: ev.guard ?? undefined,
            unconfined: ev.unconfined ?? undefined,
            risk: review?.risk,
            reviewReason: review?.reason,
            phase: "pending",
          });
        });
        return true;
      }
      patchById(assistantId, (m) => {
        // A reconnect with an old Last-Event-ID replays the park; one card per call.
        if (
          m.blocks?.some(
            (b) =>
              b.kind === "approval" &&
              b.approval.toolCallId === ev.tool_call_id,
          )
        )
          return;
        const review = findReview(m, ev.tool_call_id)?.review;
        (m.blocks ?? (m.blocks = [])).push({
          kind: "approval",
          id: `approval-${ev.tool_call_id}`,
          approval: {
            ...approvalFromEvent(ev),
            risk: review?.risk,
            reviewReason: review?.reason,
          },
        });
      });
      return true;
    }
    case "approval.decided":
      // Searched across the transcript like `question.answered`: a `message.injected`
      // boundary may have moved the fold.
      setMessages(
        produce((list) => {
          for (const m of list)
            if (settleDecision(m, ev.tool_call_id, ev.approved)) return;
        }),
      );
      return true;
    case "question.asked":
      // `questions` is defaulted once here, like `args` above.
      patchById(assistantId, (m) => {
        (m.blocks ?? (m.blocks = [])).push({
          kind: "question",
          id: `question-${ev.tool_call_id}`,
          question: {
            toolCallId: ev.tool_call_id,
            questions: (ev.questions ?? []).map((q) => ({
              question: q.question,
              multiSelect: q.multi_select ?? false,
              options: (q.options ?? []).map((o) => ({
                label: o.label,
                description: o.description ?? undefined,
              })),
            })),
          },
        });
      });
      return true;
    case "question.answered":
      // The block is kept, no longer a park. Question text rides on the event because the
      // backend pairs it from the call's arguments, as the cold load does.
      //
      // Searched across the transcript: a `message.injected` boundary retargets the fold,
      // so the question can sit on an earlier segment. The call id is unique.
      //
      // An empty list is not written, to avoid a card with a heading and no content.
      if ((ev.answers ?? []).length > 0)
        setMessages(
          produce((list) => {
            for (const m of list) {
              const block = m.blocks?.find(
                (b) =>
                  b.kind === "question" &&
                  b.question.toolCallId === ev.tool_call_id,
              );
              if (block?.kind === "question") {
                block.question.answers = (ev.answers ?? []).map((a) => ({
                  question: a.question,
                  selections: a.selections ?? [],
                  text: a.text ?? undefined,
                }));
                return;
              }
            }
          }),
        );
      return true;
    default:
      return false;
  }
}
