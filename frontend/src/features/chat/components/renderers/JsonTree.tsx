/** Renders a JSON artifact as a collapsible tree: objects/arrays expand/collapse
 *  (default open to depth 2), long arrays cap their initial render at 100 items
 *  with a mechanical "show more" row, and a parse failure falls back to the raw
 *  text. Presentation-only — the blob's bytes are parsed here, nothing else. */

import {
  createMemo,
  createResource,
  createSignal,
  For,
  Show,
  type JSX,
} from "solid-js";
import { cx, ErrorState, Icon, LoadingText, Text } from "~/ui";
import { rememberScroll } from "../../scrollMemory";
import { fontStepStyle } from "./fontStep";
import { settled } from "~/lib/resource";

type JsonValue =
  string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };

const DEFAULT_EXPAND_DEPTH = 2;
const ARRAY_PAGE = 100;
const INDENT_PX = 14;

function isContainer(
  v: JsonValue,
): v is JsonValue[] | { [key: string]: JsonValue } {
  return v !== null && typeof v === "object";
}

/** A primitive value colored by kind, monochrome-plus-dim (no accent colors —
 *  those are reserved for diffs): strings/numbers read at default brightness,
 *  `null`/booleans read dim. */
function PrimitiveValue(props: {
  value: string | number | boolean | null;
}): JSX.Element {
  return (
    <Show when={props.value !== null} fallback={<Text tone="dim">null</Text>}>
      <Show
        when={typeof props.value === "string"}
        fallback={
          <Text
            tone={typeof props.value === "number" ? "default" : "dim"}
            class="tabular-nums"
          >
            {String(props.value)}
          </Text>
        }
      >
        <Text tone="default">{JSON.stringify(props.value)}</Text>
      </Show>
    </Show>
  );
}

/** One tree node — a leaf (primitive) or a container (object/array) with its
 *  own expand/collapse + "show more" state. Every node is its own component
 *  instance keyed by its position in the (immutable, post-parse) tree, so
 *  state never leaks across values. */
function JsonNode(props: {
  value: JsonValue;
  depth: number;
  label?: string;
  trailingComma: boolean;
}): JSX.Element {
  const indent = { "padding-left": `${props.depth * INDENT_PX}px` };

  const keyPrefix = (): JSX.Element => (
    <Show when={props.label !== undefined}>
      <Text tone="bright">{JSON.stringify(props.label)}</Text>
      <Text tone="dim">{": "}</Text>
    </Show>
  );

  if (!isContainer(props.value)) {
    return (
      <div class="whitespace-pre" style={indent}>
        {keyPrefix()}
        <PrimitiveValue value={props.value} />
        <Show when={props.trailingComma}>
          <Text tone="dim">,</Text>
        </Show>
      </div>
    );
  }

  const value = props.value;
  const isArray = Array.isArray(value);
  const entries = createMemo<Array<readonly [string, JsonValue]>>(() =>
    isArray
      ? (value as JsonValue[]).map((v, i) => [String(i), v] as const)
      : Object.entries(value as { [key: string]: JsonValue }),
  );
  const count = (): number => entries().length;

  const [expanded, setExpanded] = createSignal(
    props.depth < DEFAULT_EXPAND_DEPTH,
  );
  const [visibleCount, setVisibleCount] = createSignal(
    isArray ? Math.min(count(), ARRAY_PAGE) : count(),
  );

  const openBrace = isArray ? "[" : "{";
  const closeBrace = isArray ? "]" : "}";
  const summary = (): string =>
    isArray
      ? `[…] ${count()} item${count() === 1 ? "" : "s"}`
      : `{…} ${count()} key${count() === 1 ? "" : "s"}`;

  return (
    <div>
      <div class="flex items-start gap-1 whitespace-pre" style={indent}>
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          aria-label={expanded() ? "Collapse" : "Expand"}
          aria-expanded={expanded()}
          class="shrink-0 text-dim transition-colors hover:text-bright"
        >
          <Icon
            name={expanded() ? "chevron-down" : "chevron-right"}
            size={10}
          />
        </button>
        {keyPrefix()}
        <Show
          when={expanded()}
          fallback={
            <>
              <Text tone="dim">{summary()}</Text>
              <Show when={props.trailingComma}>
                <Text tone="dim">,</Text>
              </Show>
            </>
          }
        >
          <Text tone="dim">{openBrace}</Text>
        </Show>
      </div>
      <Show when={expanded()}>
        <For each={entries().slice(0, visibleCount())}>
          {([k, v], i) => (
            <JsonNode
              value={v}
              depth={props.depth + 1}
              label={isArray ? undefined : k}
              trailingComma={i() < count() - 1}
            />
          )}
        </For>
        <Show when={isArray && visibleCount() < count()}>
          <div
            class="whitespace-pre"
            style={{ "padding-left": `${(props.depth + 1) * INDENT_PX}px` }}
          >
            <button
              type="button"
              onClick={() =>
                setVisibleCount((n) => Math.min(count(), n + ARRAY_PAGE))
              }
              class="text-dim transition-colors hover:text-bright"
            >
              <Text variant="micro" tone="dim">
                SHOW {Math.min(ARRAY_PAGE, count() - visibleCount())} MORE
              </Text>
            </button>
          </div>
        </Show>
        <div class="whitespace-pre" style={indent}>
          <Text tone="dim">{closeBrace}</Text>
          <Show when={props.trailingComma}>
            <Text tone="dim">,</Text>
          </Show>
        </div>
      </Show>
    </div>
  );
}

export function JsonTree(props: {
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

  const parsed = createMemo(
    ():
      | { ok: true; value: JsonValue }
      | { ok: false; raw: string }
      | undefined => {
      const t = settled(text);
      if (t === undefined) return undefined;
      try {
        return { ok: true, value: JSON.parse(t) as JsonValue };
      } catch {
        return { ok: false, raw: t };
      }
    },
  );

  const scrollKey = (): string => props.scrollKey ?? props.name;

  return (
    <Show
      when={!text.error}
      fallback={<ErrorState message="Could not load this file." />}
    >
      <Show when={parsed()} fallback={<LoadingText />}>
        {(p) => (
          <Text
            as="div"
            variant="code"
            ref={(el: HTMLElement) => rememberScroll(el, scrollKey)}
            class="block h-full overflow-auto bg-surface p-3"
            style={fontStepStyle(props.fontStep)}
          >
            <Show
              when={p().ok}
              fallback={
                <>
                  <Text variant="micro" tone="warn">
                    Invalid JSON — showing raw text
                  </Text>
                  <pre
                    class={cx(
                      "mt-2 text-text",
                      props.softWrap
                        ? "whitespace-pre-wrap break-words"
                        : "whitespace-pre",
                    )}
                  >
                    {(p() as { ok: false; raw: string }).raw}
                  </pre>
                </>
              }
            >
              <JsonNode
                value={(p() as { ok: true; value: JsonValue }).value}
                depth={0}
                trailingComma={false}
              />
            </Show>
          </Text>
        )}
      </Show>
    </Show>
  );
}
