/**
 * The tool family: a call's life (started, progress, completed, failed), the review the
 * chassis runs on it at Auto, and the two ways a run parks on the operator — an approval
 * and a question.
 */

import { produce } from "solid-js/store";
import type { RunEvent } from "~/lib/stream";
import {
  commandBoundary,
  commandReason,
  toTerminalOutcome,
} from "../../data/hostCommands";
import {
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
  findHost,
  findReview,
  findTool,
  upsertHost,
} from "../patch";
import type { FoldContext } from "./types";

/** Fold one tool, review or park event; false when the event is not in this family. */
export function foldTools(ev: RunEvent, c: FoldContext): boolean {
  const { deps, assistantId } = c;
  const { patchById, setMessages } = deps;
  switch (ev.type) {
    case "tool.started":
      // A command the operator watches run is a terminal, not a generic tool card.
      // Which tools those are is the table's answer, not a name test here — see
      // `ToolEntry.terminal`. (tool.started fires before approval.required, so this
      // seeds the pending terminal.) A call a script made is never one: it nests on
      // the script's card as a compact row, whatever tool it is.
      if (!ev.parent_tool_call_id && isTerminalTool(ev.name)) {
        patchById(assistantId, (m) =>
          upsertHost(m, ev.tool_call_id, ev.name, {
            command: typeof ev.args.command === "string" ? ev.args.command : "",
            explanation: commandReason(ev.args),
          }),
        );
        return true;
      }
      patchById(assistantId, (m) =>
        placeToolCall(
          m.blocks ?? (m.blocks = []),
          (id) => `tool-${id}`,
          startedTool(ev.tool_call_id, ev.name, ev.args),
          ev.parent_tool_call_id,
        ),
      );
      return true;
    case "tool.progress":
      // One frame, two readings, and **the block that already exists is what says
      // which**. A terminal is a command printing as it runs, so its frames are
      // *deltas* arriving every half-second and they APPEND — dropping the previous
      // piece would leave a terminal showing only whatever the last half-second
      // happened to print. Everything else sends a status note ("starting the
      // sandbox"), which is one fact restated, so it REPLACES.
      //
      // Reading it off the block rather than off the tool name is what keeps this
      // from being a second copy of the tool table: the event carries no name, and a
      // call's own block already encodes the answer the table gave when the call
      // started.
      patchById(assistantId, (m) => {
        const host = findHost(m, ev.tool_call_id);
        if (host) {
          if (ev.partial)
            host.command.streamed = (host.command.streamed ?? "") + ev.partial;
          // Seconds since the spawn, from the run rather than from a clock here.
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
      // A call with a result is waiting on nobody — retire the prompt it parked on,
      // so a replay doesn't re-ask what was already answered.
      patchById(assistantId, (m) => clearPark(m, ev.tool_call_id));
      if (!ev.parent_tool_call_id && isTerminalTool(ev.name)) {
        // The result is the record: it has the streams apart, where the ticks that
        // streamed in had them concatenated. So the accumulated text is dropped on
        // the same patch that replaces it — leaving it would be two copies of one
        // command's output, one of them worse.
        const outcome = toTerminalOutcome(ev.result);
        // No outcome and a sentence back means a guard refused the call outright —
        // a tool the thread cannot use, a host that cannot fence — and it never ran. The cold read
        // projects the same string the same way, so a reload agrees with what the
        // operator watched; without this arm a refused command sat lit as running
        // for the rest of the thread.
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
          // The same read the cold mapper does, so a backgrounded command's
          // declaration and fence survive a reload identically.
          t.boundary = commandBoundary(ev.result);
        }
      });
      return true;
    }
    case "tool.failed":
      // A failure settles the call too — same retirement as the completed case.
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
      // The chassis is about to answer for the operator. The row opens now rather than
      // on the verdict, so a review that costs a model call reads as work in flight —
      // and so it lands ahead of the tool row it judges, which is where it belongs.
      patchById(assistantId, (m) => {
        if (findReview(m, ev.tool_call_id)) return;
        (m.blocks ?? (m.blocks = [])).push({
          kind: "review",
          id: `review-${ev.tool_call_id}`,
          review: {
            toolCallId: ev.tool_call_id,
            name: ev.name,
            summary: ev.summary,
            // What the reviewer was given beyond the one-line summary, for the tools
            // that have any. Undefined rather than null, so the card renders the block
            // only when there is one.
            detail: ev.detail ?? undefined,
            reach: ev.reach ?? undefined,
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
        // How the call was bounded, not merely whether it was allowed: the tier says
        // which structural ground cleared it, and `fenced` says whether this host could
        // hold it there at all — the answer to "why did an ordinary command still ask?"
        b.review.tier = ev.tier ?? undefined;
        b.review.fenced = ev.fenced;
        // Null on the wire means the model stage never ran — the deterministic judge
        // cleared it, or there was nothing to review with. Undefined here so the card
        // renders the axes only when there are axes.
        b.review.risk = ev.risk ?? undefined;
        b.review.authorization = ev.authorization ?? undefined;
        b.review.correctness = ev.correctness ?? undefined;
      });
      return true;
    case "approval.required": {
      // `args` is typed as always-present, but it arrives as untrusted JSON off
      // the wire — default it once here, in the mapper, so no consumer of the
      // stored block has to guard a `Object.keys(args)` or an `args.command`.
      const args: Record<string, unknown> = ev.args ?? {};
      // At Auto the chassis tried to answer this call first, and what it found rides
      // onto whichever card ends up asking: a park is now the whole of what an
      // unrecoverable act produces, so the reviewer's `too_destructive` would otherwise
      // be visible only on a collapsed row above the prompt being answered. Both
      // surfaces take it, because the commands that earn that word are shell commands
      // and those are the ones that render as a terminal.
      if (isTerminalTool(ev.name)) {
        patchById(assistantId, (m) => {
          const review = findReview(m, ev.tool_call_id)?.review;
          upsertHost(m, ev.tool_call_id, ev.name, {
            command: typeof args.command === "string" ? args.command : "",
            explanation: ev.explanation ?? undefined,
            risk: review?.risk,
            reviewReason: review?.reason,
            phase: "pending",
          });
        });
        return true;
      }
      patchById(assistantId, (m) => {
        const review = findReview(m, ev.tool_call_id)?.review;
        (m.blocks ?? (m.blocks = [])).push({
          kind: "approval",
          id: `approval-${ev.tool_call_id}`,
          approval: {
            toolCallId: ev.tool_call_id,
            name: ev.name,
            args,
            summary: ev.summary,
            explanation: ev.explanation ?? undefined,
            risk: review?.risk,
            reviewReason: review?.reason,
          },
        });
      });
      return true;
    }
    case "question.asked":
      // Same defaulting discipline as `approval.required` above: `questions` is typed
      // as always-present but arrives as untrusted JSON, so it is defaulted once here
      // rather than guarded at every consumer of the stored block.
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
      // Marked, not removed — the block stops being a park (nothing left for the dock
      // to collect) and starts being a row in the transcript. The words of each
      // question ride on the event rather than being read off the block, because the
      // backend pairs them from the parked call's own arguments and a cold load is
      // projected from the same pairing: warm and reloaded must not be two renderings.
      //
      // Found across the transcript rather than on the fold target, for the same reason
      // the dock stopped keying its park on "the last streaming message": a
      // `message.injected` boundary retargets the fold, so the question can sit on the
      // segment *before* the one events are landing on now. Addressed by its call id,
      // which is unique, so the wider search cannot hit the wrong block.
      //
      // An empty list is deliberately not written: a malformed call that parsed to no
      // questions would otherwise be kept as a card with a heading and nothing under it.
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
