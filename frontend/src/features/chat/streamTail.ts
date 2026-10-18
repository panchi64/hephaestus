/**
 * The streaming answer's source with its unstable tail held back, so what the operator
 * reads never changes shape once it is on screen.
 *
 * `marked` lexes whatever has arrived, and an unfinished tail lexes as something other
 * than what it will become: a table row is a paragraph until its delimiter row lands,
 * `**bold` shows its asterisks until the closer, and a line that will be a list item, a
 * heading or a fence starts out as prose. Each of those reflows the text under the
 * reader's eye. Holding them costs at most a line of latency; closing open inline
 * styles early costs nothing.
 *
 * Pure. Applied only to a passage that is still streaming; the settled text renders as
 * it is.
 */

/** A partial line held until its newline: a table row, fence, HTML or math opener,
 *  none of which renders right until the line is whole. */
const WHOLE_LINE = /^ {0,3}(?:\||`{3}|~{3}|<|\$\$)/;
/** A partial line held only while it is still ambiguous — a bare list, quote or
 *  heading marker, a number that may become a list item, a rule or setext underline in
 *  progress, a fence or task box still being typed. Once content follows the marker the
 *  block type is settled and the line streams, so the reveal paces it like prose rather
 *  than fading a whole line in at once. */
const AMBIGUOUS = new RegExp(
  [
    /^ {0,3}[*+>-]\s*$/,
    /^ {0,3}[*+-]\s+\[[ xX]?\]?\s*$/,
    /^ {0,3}([*_=-])(?:\s*\1)*\s*$/,
    /^ {0,3}#{1,6}\s*$/,
    /^ {0,3}\d{1,9}[.)]?\s*$/,
    /^ {0,3}(?:`{1,2}|~{1,2})$/,
  ]
    .map((r) => r.source)
    .join("|"),
);
const FENCE = /^ {0,3}(`{3,}|~{3,})/;
const TABLE_ROW = /^ {0,3}\||\S\s*\|\s*\S/;
const TABLE_DELIMITER =
  /^ {0,3}\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?\s*$|^ {0,3}\|\s*:?-+:?\s*\|\s*$/;
/** A link still arriving at the end of a line — `[text`, `[text]` or `[text](url` —
 *  with its text captured, so the text streams as prose until the link closes. Only a
 *  bracket at a word start opens one: `arr[0]` is an index, not a link. */
const OPEN_LINK = /(?<=^|[\s(])\[([^\]]*)(?:\](?:\([^)]*)?)?$/;
/** Emphasis characters that may be the first half of a marker still arriving. */
const TRAILING_MARKERS = /[*_~]+$/;

export function stableStreamSource(text: string): string {
  const cut = text.lastIndexOf("\n") + 1;
  const complete = text.slice(0, cut);
  let partial = text.slice(cut);

  const lines = complete.split("\n");
  lines.pop();
  const state = scanBlocks(lines);
  if (state.fence) {
    // Inside an open fence marked already renders to the end of the source, and code
    // grows downward. Only a closing fence still arriving would flash as code text.
    return /^ {0,3}[`~]+\s*$/.test(partial) ? complete : text;
  }
  if (state.mathStart !== null) return lineOffset(lines, state.mathStart, text);

  if (WHOLE_LINE.test(partial) || AMBIGUOUS.test(partial)) partial = "";

  const table = trailingTable(lines, state.runStart);
  // Under a delimiter row every line until a blank one is a row, pipes or not.
  if (table?.delimited) partial = "";
  else if (table && (!partial || TABLE_ROW.test(partial)))
    return lineOffset(lines, table.start, text);

  const link = OPEN_LINK.exec(partial);
  if (link && !insideCodeSpan(partial, link.index))
    partial = partial.slice(0, link.index) + link[1];
  partial = partial.replace(TRAILING_MARKERS, "");
  // A backtick run at the very end opens a span unless it closes one.
  const ticks = /`+$/.exec(partial);
  if (ticks && !insideCodeSpan(partial, ticks.index))
    partial = partial.slice(0, ticks.index);

  const run = lines.slice(state.runStart).join("\n");
  const closers = openMarkers(
    run ? `${run}\n${partial}` : partial,
    run ? run.length + 1 : 0,
  )
    .reverse()
    .join("");
  const shown = complete + partial;
  if (!closers) return shown;
  // Before any trailing newline too: a closer opening a line is not right-flanking,
  // and marked would print it.
  const body = shown.trimEnd();
  return body + closers + shown.slice(body.length);
}

/** Open fence, open `$$` block (by line index), and where the trailing paragraph run
 *  starts — the line after the last blank one. */
function scanBlocks(lines: string[]): {
  fence: string | null;
  mathStart: number | null;
  runStart: number;
} {
  let fence: string | null = null;
  let mathStart: number | null = null;
  let runStart = 0;
  lines.forEach((raw, i) => {
    const line = raw.replace(/\r$/, "");
    const marker = FENCE.exec(line)?.[1];
    if (fence) {
      if (marker && marker[0] === fence[0] && marker.length >= fence.length)
        fence = null;
      runStart = i + 1;
      return;
    }
    if (marker && mathStart === null) {
      fence = marker;
      runStart = i + 1;
      return;
    }
    if ((line.match(/\$\$/g)?.length ?? 0) % 2 === 1)
      mathStart = mathStart === null ? i : null;
    if (!line.trim()) runStart = i + 1;
  });
  return { fence, mathStart, runStart };
}

/** The trailing run of table rows — its first line, and whether its delimiter row has
 *  arrived — or null. A prose line with a `|` in it is not a table: every line from the
 *  candidate to the end of the run has to look like a row. */
function trailingTable(
  lines: string[],
  runStart: number,
): { start: number; delimited: boolean } | null {
  const run = lines.slice(runStart).map((l) => l.replace(/\r$/, ""));
  const first = run.findIndex((l) => TABLE_ROW.test(l));
  if (first < 0) return null;
  const rows = run.slice(first);
  if (!rows.every((l) => TABLE_ROW.test(l) || TABLE_DELIMITER.test(l)))
    return null;
  return {
    start: runStart + first,
    delimited: rows.some((l) => TABLE_DELIMITER.test(l)),
  };
}

/** `text` up to the start of line `index` of `lines`. */
function lineOffset(lines: string[], index: number, text: string): string {
  let offset = 0;
  for (let i = 0; i < index; i++) offset += lines[i].length + 1;
  return text.slice(0, offset);
}

function insideCodeSpan(line: string, at: number): boolean {
  return (line.slice(0, at).match(/`/g)?.length ?? 0) % 2 === 1;
}

const isSpace = (c: string | undefined): boolean => !c || /\s/.test(c);
const isWordStart = (c: string | undefined): boolean =>
  !c || /[\s([{"'>]/.test(c);

/** Inline markers opened and not yet closed in `source`, in opening order. Code spans
 *  are skipped; a backtick run left open is itself returned. Single `*` and `_` count
 *  only from `singlesFrom` (the line still arriving): one left open on a finished line
 *  is literal, and closing it would italicise the paragraph until the run ends. */
function openMarkers(source: string, singlesFrom: number): string[] {
  const stack: string[] = [];
  let i = 0;
  while (i < source.length) {
    const c = source[i];
    if (c === "\\") {
      i += 2;
      continue;
    }
    if (c === "`") {
      const run = /^`+/.exec(source.slice(i))![0];
      const close = source.indexOf(run, i + run.length);
      if (close < 0) return [...stack, run];
      i = close + run.length;
      continue;
    }
    const pair = source.slice(i, i + 2);
    const marker =
      pair === "**" || pair === "~~" ? pair : c === "*" || c === "_" ? c : null;
    if (!marker || (marker.length === 1 && i < singlesFrom)) {
      i++;
      continue;
    }
    const before = source[i - 1];
    const after = source[i + marker.length];
    const top = stack.lastIndexOf(marker);
    if (top >= 0 && !isSpace(before)) stack.splice(top, 1);
    // A single `*` or `_` opens only at a word start, so `a*b` and snake_case stay
    // literal rather than gaining a closer marked would render as emphasis.
    else if (!isSpace(after) && (marker.length === 2 || isWordStart(before)))
      stack.push(marker);
    i += marker.length;
  }
  return stack;
}
