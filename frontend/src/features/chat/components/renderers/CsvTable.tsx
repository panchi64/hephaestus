/** Renders a CSV artifact as a sortable table: a hand-rolled parser (quoted
 *  fields, escaped quotes, CRLF), the first row as the header, click-to-sort
 *  columns (numeric-aware when every cell in the column parses as a number),
 *  and a hard cap on how many rows actually mount — with an honest banner and
 *  a download escape hatch for the full file. Presentation-only: the blob's
 *  bytes are parsed here, nothing is sent anywhere. */

import {
  createMemo,
  createResource,
  createSignal,
  For,
  Show,
  type JSX,
} from "solid-js";
import {
  cx,
  Button,
  EmptyState,
  ErrorState,
  Icon,
  LoadingText,
  Text,
} from "~/ui";
import { rememberScroll } from "../../scrollMemory";
import { downloadBlob } from "../../viewport/downloadRegistry";
import { fontStepStyle } from "./fontStep";
import { settled } from "~/lib/resource";

const ROW_CAP = 5_000;

/** Hand-rolled CSV parser: quoted fields, `""` escaped quotes inside a quoted
 *  field, and CRLF/LF line endings. No dependency — the format is simple
 *  enough that a lightweight state machine is more honest than a library. */
function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let inQuotes = false;
  let sawAny = false;

  const endField = (): void => {
    row.push(field);
    field = "";
  };
  const endRow = (): void => {
    endField();
    rows.push(row);
    row = [];
  };

  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inQuotes) {
      if (c === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i++;
        } else {
          inQuotes = false;
        }
      } else {
        field += c;
      }
      sawAny = true;
      continue;
    }
    if (c === '"') {
      inQuotes = true;
      sawAny = true;
    } else if (c === ",") {
      endField();
      sawAny = true;
    } else if (c === "\r") {
      // Swallowed; the paired \n (or a lone \r, rare) ends the row below.
      continue;
    } else if (c === "\n") {
      endRow();
      sawAny = false;
    } else {
      field += c;
      sawAny = true;
    }
  }
  // A trailing newline leaves nothing pending — don't emit a phantom last row.
  if (sawAny || field.length > 0 || row.length > 0) endRow();

  return rows;
}

const NUMERIC_RE = /^[+-]?\d+(\.\d+)?([eE][+-]?\d+)?$/;

function isNumericColumn(rows: string[][], col: number): boolean {
  let sawValue = false;
  for (const r of rows) {
    const v = (r[col] ?? "").trim();
    if (v === "") continue;
    sawValue = true;
    if (!NUMERIC_RE.test(v)) return false;
  }
  return sawValue;
}

type SortDir = "asc" | "desc";

export function CsvTable(props: {
  data: Blob;
  name: string;
  fontStep?: number;
  softWrap?: boolean;
  scrollKey?: string;
}): JSX.Element {
  const [text] = createResource(
    () => props.data,
    (blob) => blob.text(),
  );

  const rows = createMemo(() => parseCsv(settled(text) ?? ""));
  const header = createMemo(() => rows()[0] ?? []);
  const body = createMemo(() => rows().slice(1));

  const [sortCol, setSortCol] = createSignal<number | null>(null);
  const [sortDir, setSortDir] = createSignal<SortDir>("asc");

  // asc -> desc -> none, per column. Clicking a different column starts fresh at asc.
  const cycleSort = (col: number): void => {
    if (sortCol() !== col) {
      setSortCol(col);
      setSortDir("asc");
    } else if (sortDir() === "asc") {
      setSortDir("desc");
    } else {
      setSortCol(null);
    }
  };

  const sortedBody = createMemo(() => {
    const col = sortCol();
    if (col === null) return body();
    const dir = sortDir();
    const numeric = isNumericColumn(body(), col);
    const sign = dir === "asc" ? 1 : -1;
    return [...body()].sort((a, b) => {
      const av = (a[col] ?? "").trim();
      const bv = (b[col] ?? "").trim();
      const cmp = numeric
        ? (parseFloat(av) || 0) - (parseFloat(bv) || 0)
        : av.localeCompare(bv);
      return cmp * sign;
    });
  });

  const visibleRows = createMemo(() => sortedBody().slice(0, ROW_CAP));
  const truncated = createMemo(() => sortedBody().length > ROW_CAP);
  const scrollKey = (): string => props.scrollKey ?? props.name;
  const cellWrap = (): string =>
    props.softWrap ? "whitespace-pre-wrap break-words" : "whitespace-nowrap";

  return (
    <Show
      when={!text.error}
      fallback={<ErrorState message="Could not load this file." />}
    >
      <Show when={settled(text) !== undefined} fallback={<LoadingText />}>
        <div class="flex h-full min-h-0 flex-col">
          <Show when={truncated()}>
            <div class="flex shrink-0 items-center justify-between gap-2 px-2 py-1.5">
              <Text variant="micro" tone="warn">
                {`SHOWING FIRST ${ROW_CAP.toLocaleString()} OF ${sortedBody().length.toLocaleString()} ROWS — DOWNLOAD FOR THE FULL FILE`}
              </Text>
              <Button
                variant="ghost"
                size="sm"
                leading="download"
                onClick={() => downloadBlob(props.name, props.data)}
              >
                Download
              </Button>
            </div>
          </Show>
          <Show
            when={header().length > 0}
            fallback={<EmptyState message="No data" />}
          >
            <Text
              as="div"
              variant="code"
              ref={(el: HTMLElement) => rememberScroll(el, scrollKey)}
              class="min-h-0 flex-1 overflow-auto bg-surface"
              style={fontStepStyle(props.fontStep)}
            >
              <table class="w-full border-collapse">
                <thead>
                  <tr>
                    <For each={header()}>
                      {(label, i) => (
                        <th class="sticky top-0 z-10 border border-line bg-surface px-2 py-1 text-left align-top">
                          <button
                            type="button"
                            onClick={() => cycleSort(i())}
                            class="flex items-center gap-1 transition-colors hover:text-bright"
                          >
                            <Text
                              tone={sortCol() === i() ? "bright" : "dim"}
                              class="truncate"
                            >
                              {label || " "}
                            </Text>
                            <Show when={sortCol() === i()}>
                              <Icon
                                name={
                                  sortDir() === "asc"
                                    ? "chevron-up"
                                    : "chevron-down"
                                }
                                size={10}
                                class="shrink-0 text-dim"
                              />
                            </Show>
                          </button>
                        </th>
                      )}
                    </For>
                  </tr>
                </thead>
                <tbody>
                  <For each={visibleRows()}>
                    {(row) => (
                      <tr>
                        <For each={header()}>
                          {(_, i) => (
                            <td
                              class={cx(
                                "border border-line px-2 py-1 align-top text-text",
                                cellWrap(),
                              )}
                            >
                              {row[i()] ?? ""}
                            </td>
                          )}
                        </For>
                      </tr>
                    )}
                  </For>
                </tbody>
              </table>
            </Text>
          </Show>
        </div>
      </Show>
    </Show>
  );
}
