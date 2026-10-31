/** Two one-liners about a tool call: what it was *about*, and what came *back*.
 *
 *  A row that says only which tool ran tells the operator almost nothing — a turn
 *  with six `files_read_file` calls is six identical rows. The salient argument is
 *  what distinguishes them, and the result's shape is what says whether the call
 *  landed. Both are derived here, in the mapping layer, because both need the raw
 *  payloads: by the time a `ToolInvocation` exists the args are a flat string and
 *  the result is stringified JSON.
 *
 *  **Both return `undefined` rather than guessing.** No salient argument means the
 *  card falls back to the full `k=v` summary — today's behavior — and no readable
 *  result means no outcome segment at all. A row that says nothing is better than
 *  a row that says something wrong, and the payloads here are provider- and
 *  tool-shaped, so "nothing matched" is a normal outcome, not a defect. */

import { toolEntry } from "./toolPresentation";

/** Argument keys in preference order, for the tools whose salient argument the
 *  table doesn't name — including every `external_*` connector action, whose
 *  parameters come from the operator's own integration and can't be enumerated. */
const GENERIC_KEYS = [
  "path",
  "file_path",
  "file",
  "command",
  "query",
  "question",
  "url",
  "pattern",
  "task",
  "title",
  "name",
  "subject",
  "content",
  "text",
  "code",
  "prompt",
  "message",
  "body",
  "explanation",
  "description",
  "reason",
  "id",
] as const;

/** Wide enough for a repo-relative path or a real shell command; the row truncates
 *  with CSS anyway, so this only bounds what a copy of the DOM would carry. */
const MAX_DETAIL = 120;
/** Outcomes sit to the right of the detail and must not crowd it out. */
const MAX_OUTCOME = 72;

const DEFAULT_NOUN = ["result", "results"] as const;

function clamp(text: string, max: number): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat;
}

/** A scalar argument as one line. A multi-line value (a code blob, a mail body)
 *  becomes its first non-empty line — the rest is the payload, not the summary. */
function display(value: unknown): string | undefined {
  if (value == null || value === "") return undefined;
  if (Array.isArray(value)) {
    const parts = value.flatMap((v) => display(v) ?? []);
    return parts.length ? clamp(parts.join(", "), MAX_DETAIL) : undefined;
  }
  if (typeof value === "object") return undefined;
  return firstLine(String(value));
}

/** The first non-empty line of `text`, flattened and clamped to a row's detail length. */
export function firstLine(text: string): string | undefined {
  const line = text.split("\n").find((l) => l.trim().length > 0);
  return line ? clamp(line, MAX_DETAIL) : undefined;
}

/** The argument the backend injects to carry the model's own reason for a call.
 *
 *  Named once here because three places need to agree about it and none of them
 *  should be the one that knows: the row that promotes it, the expanded dump that
 *  must not print it twice, and the summary that puts it in a fold's headline. */
export const NARRATION_ARG = "narration";

/** Why the model says it is making this call. One sentence, or nothing.
 *
 *  Clamped like any other detail: it arrives as free text from a model, and a row
 *  is a row however long the sentence turned out to be. */
export function toolNarration(
  args: Record<string, unknown>,
): string | undefined {
  const value = args[NARRATION_ARG];
  return typeof value === "string" ? display(value) : undefined;
}

/** The one argument that says what this call is about — "backend/app.py" for a
 *  read, the command for a shell run, the query for a search. */
export function describeToolArgs(
  name: string,
  args: Record<string, unknown>,
): string | undefined {
  const keys = toolEntry(name).keys ?? GENERIC_KEYS;
  for (const key of keys) {
    const shown = display(args[key]);
    if (shown !== undefined) return shown;
  }
  return undefined;
}

function count(
  n: number,
  noun: readonly [string, string] = DEFAULT_NOUN,
): string {
  return `${n} ${n === 1 ? noun[0] : noun[1]}`;
}

/** A structured return: an exit status, a reported failure, or a collection. */
function fromRecord(
  record: Record<string, unknown>,
  noun: readonly [string, string] | undefined,
): string | undefined {
  if (typeof record.exit_code === "number") {
    const exit = `exit ${record.exit_code}`;
    return record.timed_out === true ? `${exit} · timed out` : exit;
  }
  // A tool that returns `{error: ...}` still *completed* — the card's error branch
  // never fires, so this is the only place the operator would see it.
  if (typeof record.error === "string" && record.error.trim())
    return clamp(record.error, MAX_OUTCOME);
  const lists = Object.values(record).filter(Array.isArray);
  if (lists.length === 1) return count(lists[0].length, noun);
  if (typeof record.count === "number") return count(record.count, noun);
  if (typeof record.total === "number") return count(record.total, noun);
  return undefined;
}

/** Lines in `text` between `start` and its last non-space character — the count
 *  `trim().split("\n").length` gives, without the two copies it makes of the
 *  whole string. A shell command's output is capped at two million characters
 *  and a file read is a whole file, so the obvious form would allocate tens of
 *  thousands of substrings — on the main thread, inside the stream handler — to
 *  produce three words. */
function lineCount(text: string, start: number): number {
  let end = text.length;
  while (end > start && /\s/.test(text[end - 1])) end--;
  let lines = 1;
  for (
    let i = text.indexOf("\n", start);
    i !== -1 && i < end;
    i = text.indexOf("\n", i + 1)
  )
    lines++;
  return lines;
}

/** What the call produced, in a few characters: "12 entries", "exit 0", or the
 *  answer itself when it is short enough to simply show. */
export function describeToolResult(
  name: string,
  result: unknown,
): string | undefined {
  const { noun } = toolEntry(name);
  if (Array.isArray(result)) return count(result.length, noun);
  if (typeof result === "number") return String(result);
  if (typeof result === "string") {
    // `search` stops at the first non-space and allocates nothing; `trim()` here
    // would copy the entire payload just to ask whether it is blank.
    const start = result.search(/\S/);
    if (start === -1) return undefined;
    const lines = lineCount(result, start);
    return lines === 1
      ? clamp(result.slice(start), MAX_OUTCOME)
      : count(lines, ["line", "lines"]);
  }
  if (result !== null && typeof result === "object")
    return fromRecord(result as Record<string, unknown>, noun);
  return undefined;
}
