import { PLUGIN_ENVELOPES } from "~/plugins/registry";
import type { ContentPart } from "./model";

export interface EnvelopeLabel {
  /** The kind, in the machine voice: the word that carries the distinction. */
  label: string;
  /** Where it came from — a source, a path, a count. */
  detail?: string;
}

/** Where a part sits. Inside a user turn an `untrusted` part is attachment text or one
 *  a plugin labels; inside a tool result it is whatever the tool fetched. */
export type EnvelopeScope = "tool" | "message";

export function envelopeLabel(
  part: ContentPart,
  scope: EnvelopeScope,
): EnvelopeLabel {
  if (part.kind === "context_file")
    return { label: "Project instructions", detail: part.path };
  if (part.source && Object.hasOwn(PLUGIN_ENVELOPES, part.source))
    return PLUGIN_ENVELOPES[part.source];
  if (scope === "message")
    return { label: "Attachment text", detail: part.source };
  return { label: "External", detail: part.source };
}

/** How many tasks an `open-tasks` reminder lists: its bullet lines. */
export function openTaskCount(body: string): number {
  return (body.match(/^\s*- /gm) ?? []).length;
}

export function noticeLabel(
  kind: string | undefined,
  body: string,
): EnvelopeLabel {
  if (kind === "open-tasks") {
    const n = openTaskCount(body);
    return {
      label: "Reminder",
      detail: `${n} ${n === 1 ? "task" : "tasks"} open`,
    };
  }
  return { label: "Notice", detail: kind };
}
