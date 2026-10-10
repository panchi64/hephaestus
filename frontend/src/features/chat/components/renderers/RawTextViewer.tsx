/** Renders a big raw text/log artifact: a bounded slice of the blob, hand-rolled
 *  row virtualization (no dependency — the file may be hundreds of thousands of
 *  lines), a dim line-number gutter, and a small case-insensitive substring search
 *  that cycles matches. Presentation-only: the blob and its bytes come from the
 *  caller; this never fetches or decides anything on its own. */

import {
  createEffect,
  createMemo,
  createResource,
  createSignal,
  For,
  onCleanup,
  Show,
  type JSX,
} from "solid-js";
import {
  cx,
  EmptyState,
  ErrorState,
  Icon,
  Input,
  LoadingText,
  Text,
} from "~/ui";
import { rememberScroll } from "../../scrollMemory";
import { fontStepMetrics, fontStepStyle } from "./fontStep";
import { settled } from "~/lib/resource";

const MAX_BYTES = 10 * 1024 * 1024; // 10 MB
const MAX_LINES = 200_000;
// Rendering every wrapped line without virtualization is O(n) DOM nodes — fine up
// to a few thousand rows, but hundreds of thousands would jank scrolling and
// layout. Wrapped rows have variable height (can't be windowed with fixed-height
// math), so instead of a second virtualization scheme we just cap what's rendered.
const MAX_WRAP_LINES = 5_000;
const OVERSCAN_ROWS = 8;

interface LoadedText {
  lines: string[];
  truncatedBytes: boolean;
  originalBytes: number;
}

async function loadText(blob: Blob): Promise<LoadedText> {
  const truncatedBytes = blob.size > MAX_BYTES;
  const slice = truncatedBytes ? blob.slice(0, MAX_BYTES) : blob;
  const text = await slice.text();
  const lines = text.split("\n");
  return { lines, truncatedBytes, originalBytes: blob.size };
}

function bytesLabel(n: number): string {
  if (n >= 1024 * 1024 * 1024)
    return `${(n / (1024 * 1024 * 1024)).toFixed(1)} GB`;
  if (n >= 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  if (n >= 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${n} B`;
}

export function RawTextViewer(props: {
  data: Blob;
  name: string;
  fontStep?: number;
  softWrap?: boolean;
  scrollKey?: string;
}): JSX.Element {
  const [loaded] = createResource(() => props.data, loadText);

  const font = createMemo(() => fontStepMetrics(props.fontStep));

  const lines = createMemo(() => {
    const l = settled(loaded);
    if (!l) return [];
    return l.lines.length > MAX_LINES ? l.lines.slice(0, MAX_LINES) : l.lines;
  });
  const lineCountTruncated = createMemo(
    () => (settled(loaded)?.lines.length ?? 0) > MAX_LINES,
  );

  const wrapCapped = createMemo(
    () => Boolean(props.softWrap) && lines().length > MAX_WRAP_LINES,
  );
  const renderLines = createMemo(() =>
    wrapCapped() ? lines().slice(0, MAX_WRAP_LINES) : lines(),
  );

  const gutterChars = createMemo(() => String(renderLines().length).length);

  // ── search ──────────────────────────────────────────────────────────────
  const [query, setQuery] = createSignal("");
  const matches = createMemo(() => {
    const q = query().trim().toLowerCase();
    if (!q) return [] as number[];
    const arr = renderLines();
    const out: number[] = [];
    for (let i = 0; i < arr.length; i++) {
      if (arr[i].toLowerCase().includes(q)) out.push(i);
    }
    return out;
  });
  const [matchIdx, setMatchIdx] = createSignal(0);
  createEffect(() => {
    query();
    setMatchIdx(0);
  });
  const currentMatchLine = createMemo(() => {
    const m = matches();
    return m.length ? m[matchIdx() % m.length] : null;
  });

  const goToMatch = (delta: number): void => {
    const m = matches();
    if (m.length === 0) return;
    setMatchIdx((i) => (i + delta + m.length) % m.length);
  };

  // ── virtualization (non-wrap arm) ──────────────────────────────────────
  let containerEl: HTMLElement | undefined;
  const [scrollTop, setScrollTop] = createSignal(0);
  const [viewportH, setViewportH] = createSignal(0);

  const rowH = createMemo(() => font().line);
  const visibleRange = createMemo(() => {
    const h = rowH();
    const total = renderLines().length;
    const start = Math.max(0, Math.floor(scrollTop() / h) - OVERSCAN_ROWS);
    const visibleCount = Math.ceil(viewportH() / h) + OVERSCAN_ROWS * 2;
    const end = Math.min(total, start + visibleCount);
    const out: number[] = [];
    for (let i = start; i < end; i++) out.push(i);
    return out;
  });

  const maxChars = createMemo(() =>
    renderLines().reduce((m, l) => Math.max(m, l.length), 1),
  );

  const scrollKey = (): string => props.scrollKey ?? `raw:${props.name}`;

  createEffect(() => {
    const line = currentMatchLine();
    if (line === null || !containerEl || props.softWrap) return;
    const h = rowH();
    const target = line * h - viewportH() / 2 + h / 2;
    containerEl.scrollTop = Math.max(0, target);
  });

  createEffect(() => {
    const line = currentMatchLine();
    if (line === null || !props.softWrap || !containerEl) return;
    const row = containerEl.querySelector<HTMLElement>(`[data-line="${line}"]`);
    row?.scrollIntoView({ block: "center" });
  });

  let resizeObserver: ResizeObserver | undefined;
  const attachContainer = (el: HTMLElement): void => {
    // softWrap toggling swaps `<Show>` branches and calls this a second time on
    // the same live component instance — disconnect any prior observer first so
    // it isn't leaked.
    resizeObserver?.disconnect();
    containerEl = el;
    setViewportH(el.clientHeight);
    resizeObserver = new ResizeObserver(() => setViewportH(el.clientHeight));
    resizeObserver.observe(el);
    rememberScroll(el, scrollKey);
  };
  onCleanup(() => resizeObserver?.disconnect());

  const onScroll = (e: Event): void => {
    setScrollTop((e.currentTarget as HTMLDivElement).scrollTop);
  };

  const onSearchKeydown = (e: KeyboardEvent): void => {
    if (e.key === "Enter") {
      e.preventDefault();
      goToMatch(e.shiftKey ? -1 : 1);
    }
  };

  const truncationBanner = (): string | null => {
    const l = settled(loaded);
    if (!l) return null;
    if (l.truncatedBytes) {
      return `SHOWING FIRST ${bytesLabel(MAX_BYTES)} OF ${bytesLabel(l.originalBytes)} — DOWNLOAD FOR THE FULL FILE`;
    }
    if (lineCountTruncated()) {
      return `SHOWING FIRST ${MAX_LINES.toLocaleString()} LINES — DOWNLOAD FOR THE FULL FILE`;
    }
    if (wrapCapped()) {
      return `WRAP VIEW CAPPED AT ${MAX_WRAP_LINES.toLocaleString()} LINES — TOGGLE WRAP OFF TO SEE MORE`;
    }
    return null;
  };

  return (
    <Show
      when={!loaded.error}
      fallback={<ErrorState message="Could not load this file." />}
    >
      <Show
        when={settled(loaded)}
        fallback={<LoadingText label="Loading view…" />}
      >
        <div class="flex h-full min-h-0 flex-col">
          <div class="flex shrink-0 items-center gap-2 px-2 py-1.5">
            <Input
              leading="search"
              placeholder="Search…"
              aria-label="Search file contents"
              value={query()}
              onInput={(e) => setQuery(e.currentTarget.value)}
              onKeyDown={onSearchKeydown}
              class="h-6 max-w-48"
            />
            <button
              type="button"
              class="text-dim transition-colors hover:text-bright disabled:opacity-40"
              disabled={matches().length === 0}
              onClick={() => goToMatch(-1)}
              aria-label="Previous match"
            >
              <Icon name="chevron-up" size={14} />
            </button>
            <button
              type="button"
              class="text-dim transition-colors hover:text-bright disabled:opacity-40"
              disabled={matches().length === 0}
              onClick={() => goToMatch(1)}
              aria-label="Next match"
            >
              <Icon name="chevron-down" size={14} />
            </button>
            <Text variant="micro" tone="dim" class="tabular-nums">
              {matches().length > 0
                ? `${matchIdx() + 1}/${matches().length}`
                : "0/0"}
            </Text>
          </div>
          <Show when={truncationBanner()}>
            {(banner) => (
              <div class="shrink-0 px-2 py-1">
                <Text variant="micro" tone="warn">
                  {banner()}
                </Text>
              </div>
            )}
          </Show>
          <Show
            when={renderLines().length > 0}
            fallback={<EmptyState message="Empty file" />}
          >
            <Show
              when={!props.softWrap}
              fallback={
                <Text
                  as="div"
                  variant="code"
                  ref={attachContainer}
                  class="block h-full min-h-0 overflow-auto"
                  style={fontStepStyle(props.fontStep)}
                >
                  <table class="w-full border-collapse">
                    <tbody>
                      <For each={renderLines()}>
                        {(line, i) => (
                          <tr data-line={i()}>
                            <td
                              class="select-none whitespace-nowrap px-2 py-0 text-right align-top text-dim tabular-nums"
                              style={{ width: `${gutterChars()}ch` }}
                            >
                              {i() + 1}
                            </td>
                            <td
                              class={cx(
                                "w-full whitespace-pre-wrap break-all py-0 pr-2 align-top text-text",
                                currentMatchLine() === i() &&
                                  "bg-raised text-bright",
                              )}
                            >
                              {line || " "}
                            </td>
                          </tr>
                        )}
                      </For>
                    </tbody>
                  </table>
                </Text>
              }
            >
              <Text
                as="div"
                variant="code"
                ref={attachContainer}
                class="relative block h-full min-h-0 overflow-auto"
                style={fontStepStyle(props.fontStep)}
                onScroll={onScroll}
              >
                <div
                  class="relative"
                  style={{
                    height: `${renderLines().length * rowH()}px`,
                    width: `${maxChars() + gutterChars() + 2}ch`,
                  }}
                >
                  <For each={visibleRange()}>
                    {(i) => (
                      <div
                        class="absolute left-0 flex w-full whitespace-pre"
                        style={{
                          top: `${i * rowH()}px`,
                          height: `${rowH()}px`,
                        }}
                      >
                        <span
                          class="select-none shrink-0 px-2 text-right text-dim tabular-nums"
                          style={{ width: `${gutterChars()}ch` }}
                        >
                          {i + 1}
                        </span>
                        <span
                          class={cx(
                            "pr-2 text-text",
                            currentMatchLine() === i && "bg-raised text-bright",
                          )}
                        >
                          {renderLines()[i] || " "}
                        </span>
                      </div>
                    )}
                  </For>
                </div>
              </Text>
            </Show>
          </Show>
        </div>
      </Show>
    </Show>
  );
}
