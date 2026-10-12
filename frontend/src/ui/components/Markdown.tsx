import {
  createEffect,
  createMemo,
  For,
  onCleanup,
  Show,
  splitProps,
  type JSX,
} from "solid-js";
import { marked, type Token } from "marked";
import "katex/dist/katex.min.css";
import { cx } from "../cx";
import { copyToClipboard } from "../clipboard";
import { openHostPath } from "~/lib/hostOpen";
import { markedLinks } from "./markdownLinks";
import { markedMath } from "./markdownMath";
import { markedMermaid } from "./markdownMermaid";
import { hydrateRemoteImages } from "./remoteImages";
import { disposeMermaid, hydrateMermaid } from "./hydrateMermaid";

export interface MarkdownProps {
  /** Markdown source. Rendered with token-styled prose (.ody-prose). */
  children: string;
  class?: string;
  /** Add a hover copy button to each rendered code block. Default true. */
  copyCode?: boolean;
  /** Opt-in streaming-stable rendering: source is split into top-level blocks
   *  (`marked.lexer`) and each block is rendered/cached independently, so an
   *  unchanged prefix of blocks keeps its DOM node across streaming deltas
   *  instead of the whole body re-parsing and swapping wholesale (which is
   *  what causes KaTeX/markdown flicker mid-stream). Default false — the
   *  default path is unchanged. */
  streamStable?: boolean;
  /** Headings one step down the reading scale (`.ody-prose-dense`), for a document
   *  read in a narrow pane where full-size headings outshout the pane's own title.
   *  Body text keeps the reading size. Default false. */
  dense?: boolean;
}

marked.setOptions({ gfm: true, breaks: true });
marked.use(markedMath);
marked.use(markedMermaid);
marked.use(markedLinks);

/** Top-level block sources for `source`, in document order — the exact strings
 *  `marked.lexer` re-serializes each top-level token from. Reused by callers that
 *  need to reason about "blocks" outside this component (e.g. mapping a changed
 *  line number to a block index for a passage anchor). */
export function markdownBlocks(source: string): string[] {
  return marked.lexer(source ?? "").map((t) => t.raw);
}

/** Module-level cache: block raw source -> parsed HTML, shared across every
 *  `streamStable` Markdown instance. Capped so a very long session can't grow it
 *  unbounded; eviction is oldest-first (Map preserves insertion order). */
const BLOCK_PARSE_CACHE_CAP = 500;
const blockParseCache = new Map<string, string>();

function cachedParse(raw: string): string {
  const hit = blockParseCache.get(raw);
  if (hit !== undefined) return hit;
  const html = marked.parse(raw, { async: false }) as string;
  blockParseCache.set(raw, html);
  if (blockParseCache.size > BLOCK_PARSE_CACHE_CAP) {
    const oldest = blockParseCache.keys().next().value;
    if (oldest !== undefined) blockParseCache.delete(oldest);
  }
  return html;
}

/** Vertical rhythm between top-level blocks, replicating theme.css's heading
 *  cascade at the block-wrapper level — the `streamStable` path wraps each block in
 *  its own `div`, so those wrappers, not the raw `<p>`/`<h*>` elements, are the
 *  direct children `.ody-prose`'s selectors key off, and the heading-aware rules
 *  never see them.
 *
 *  **This is a transcription, not a second opinion.** Every value below is one of
 *  theme.css's, and the two drift silently when they disagree: the same answer takes
 *  one rhythm while it streams and another once it reloads from history, which is
 *  invisible until someone reloads a thread mid-read. The mapping, in order —
 *  `h1` → `space-8`, `h2`/`h3` → `space-6`, `h4`–`h6` → `space-5`, the block after
 *  any heading → `space-2` (tight, so the heading binds to what it opens), anything
 *  else → `space-4`. Change one side and change this one. */
function headingDepth(token: Token | undefined): number | null {
  return token?.type === "heading" ? token.depth : null;
}

function blockSpacingClass(
  prev: Token | undefined,
  curr: Token,
  isFirst: boolean,
): string {
  if (isFirst) return "mt-0";
  const currDepth = headingDepth(curr);
  if (currDepth === 1) return "mt-8";
  if (currDepth === 2 || currDepth === 3) return "mt-6";
  if (currDepth !== null) return "mt-5";
  if (headingDepth(prev) !== null) return "mt-2";
  return "mt-4";
}

/** Token-classed copy affordance injected into the top-right of each `pre`. Built
 *  as a detached node (not innerHTML) so the markup stays theme-safe and the click
 *  is handled by delegation rather than inline scripting. */
function makeCopyButton(): HTMLButtonElement {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.dataset.codeCopy = "";
  btn.setAttribute("aria-label", "Copy code");
  btn.className =
    "ody-code-copy absolute right-1 top-1 hidden border border-line bg-raised px-2 py-0.5 text-micro uppercase tracking-label text-dim transition-colors hover:text-bright group-hover/code:block focus:block focus:outline-none";
  btn.textContent = "Copy";
  return btn;
}

/**
 * Renders markdown as structured, token-styled prose. Used for assistant
 * replies, reports, and document bodies. Each fenced code block gets a
 * hover copy button (top-right) that copies the block's clean source — no fences.
 *
 * The source is model- or user-authored and the result is injected as HTML, so
 * the three constructs that could carry a URL or markup out of the source and
 * into the DOM are closed at the renderer (`markdownLinks`): link hrefs are
 * scheme-checked and escaped, images degrade to their alt text rather than
 * fetching, and raw HTML is escaped to visible text rather than injected.
 * Everything else `marked` emits is markup it wrote itself from Markdown tokens,
 * so there is nothing left for a sanitizer pass to take out.
 *
 * The one exception is a ```` ```mermaid ```` fence, which is markup written by
 * something other than `marked` — a layout engine, from model-authored source.
 * It is the one construct here that does get a sanitizer pass, and it gets two:
 * see `hydrateMermaid`. Nothing it produces is ever `innerHTML`-ed.
 *
 * The one control in here that acts on the *host* — a path the answer pointed
 * at, opened in the operator's editor — carries no authority from this side. It
 * arrives as a `data-open-path` string and the backend decides whether it names
 * a file the operator's projects contain; the click below only relays it.
 */
export function Markdown(props: MarkdownProps): JSX.Element {
  const [local] = splitProps(props, [
    "children",
    "class",
    "copyCode",
    "streamStable",
    "dense",
  ]);
  const proseClass = (): string =>
    cx("ody-prose", local.dense && "ody-prose-dense", local.class);
  // Gated on the path that actually renders it. Solid memos are eager, so an
  // ungated one re-parsed the *whole* answer on every delta while streaming — the
  // one mode where the result is thrown away, since the block path below renders
  // from `blockRaws()` instead. That is a full re-lex and re-render of everything
  // received so far, per token, for nothing.
  const html = createMemo(() =>
    local.streamStable
      ? ""
      : (marked.parse(local.children ?? "", { async: false }) as string),
  );
  // Only computed/tracked in streamStable mode — the default path never lexes.
  const tokens = createMemo(() =>
    local.streamStable ? marked.lexer(local.children ?? "") : [],
  );
  // The `<For>` iterates raw strings, not token objects: `marked.lexer` mints a
  // fresh token object per call, so iterating tokens directly would make every
  // block look "new" on every delta (by reference) and defeat the whole point.
  // Raw source strings for an unchanged prefix are `===`-equal by value across
  // recomputes, which is what lets `<For>` keep that block's DOM node (and its
  // already-rendered KaTeX) untouched while only the trailing block re-renders.
  const blockRaws = createMemo(() => tokens().map((t) => t.raw));

  let ref: HTMLDivElement | undefined;

  // Post-render enhancement, idempotent per node, re-run whenever the rendered
  // HTML changes (streaming answers re-parse on every delta):
  //   • wrap each <pre> in a relative `group/code` host + copy button
  //   • wrap each <table> in a scroll host so a wide table scrolls horizontally
  //     instead of bursting its container
  // Scans the whole container regardless of path — cheap (idempotency check is a
  // single dataset read per pre/table) and correct for the block path, where only
  // the trailing block's DOM actually changed per delta.
  const enhance = (): void => {
    if (!ref) return;
    // Unconditional, and ahead of the `copyCode` gate below: a remote image has no
    // `src` until this runs (markdownLinks parks the address so nothing loads on
    // parse), so gating it on a code-affordance flag would leave the image blank
    // for any caller that turned copy buttons off.
    hydrateRemoteImages(ref);
    // Same reasoning, one construct along: `markdownMermaid` emits an empty
    // placeholder so the parse stays synchronous and mermaid stays out of the
    // bundle, which leaves the drawing itself to this pass. Unconditional and
    // ahead of the `copyCode` gate for the same reason the images are — a
    // diagram is content, not a code affordance.
    hydrateMermaid(ref);
    if (local.copyCode === false) return;
    const pres = ref.querySelectorAll<HTMLPreElement>("pre");
    pres.forEach((pre) => {
      if (pre.parentElement?.dataset.codeHost !== undefined) return;
      const host = document.createElement("div");
      host.dataset.codeHost = "";
      host.className = "group/code relative";
      pre.replaceWith(host);
      host.appendChild(pre);
      host.appendChild(makeCopyButton());
    });
    const tables = ref.querySelectorAll<HTMLTableElement>("table");
    tables.forEach((table) => {
      if (table.parentElement?.dataset.tableHost !== undefined) return;
      const host = document.createElement("div");
      host.dataset.tableHost = "";
      table.replaceWith(host);
      host.appendChild(table);
    });
  };

  createEffect(() => {
    // Re-runs when `copyCode` is toggled (stream end), which is the point: the pass
    // has to be re-scheduled so blocks rendered while it was off get their buttons.
    void local.copyCode;
    // Track re-parses (streaming deltas) so new blocks get enhanced.
    if (local.streamStable) blockRaws();
    else html();
    // Always scheduled, even with `copyCode` off. It used to be skipped there to
    // spare a long stream one DOM scan per token, but the pass no longer only adds
    // affordances — it is also what gives a remote image its `src`, and an image
    // that never loads is a worse trade than a `querySelectorAll` per delta.
    queueMicrotask(enhance);
  });

  // One delegated click handler for both affordances rendered into this prose —
  // the copy button on a code block, and a path the answer pointed at. Attached
  // once to the outer container, so it covers blocks that arrive mid-stream and
  // costs one listener rather than one per rendered control.
  const onClick = (e: MouseEvent): void => {
    const target = e.target as HTMLElement;
    const opener = target.closest<HTMLElement>("[data-open-path]");
    if (opener) {
      // Fire-and-forget: `openHostPath` reports its own failures, and awaiting
      // an editor launch would hold the handler open for nothing.
      void openHostPath(opener.dataset.openPath ?? "");
      return;
    }
    const btn = target.closest<HTMLButtonElement>("[data-code-copy]");
    if (!btn) return;
    const code = btn.parentElement?.querySelector("pre code, pre");
    copyToClipboard(code?.textContent ?? "", "Code");
  };

  onCleanup(() => {
    ref?.removeEventListener("click", onClick);
    // Each diagram `hydrateMermaid` mounted owns a detached `render()` root, and
    // the hydrator only reaps disconnected ones on its *next* pass. Nothing
    // guarantees there is a next pass — leaving a thread for Settings renders no
    // further markdown — so the component that asked for them takes them down.
    if (ref) disposeMermaid(ref);
  });

  // Branch the whole element rather than conditionally setting `innerHTML`
  // alongside JSX children on one node (Solid's `innerHTML` prop writes the DOM
  // property directly — it can't coexist with rendered children on the same
  // node). Keeps the default path's element byte-identical to before.
  return (
    <Show
      when={local.streamStable}
      fallback={
        <div
          ref={(el) => {
            ref = el;
            el.addEventListener("click", onClick);
            queueMicrotask(enhance);
          }}
          class={proseClass()}
          innerHTML={html()}
        />
      }
    >
      <div
        ref={(el) => {
          ref = el;
          el.addEventListener("click", onClick);
          queueMicrotask(enhance);
        }}
        class={proseClass()}
      >
        <For each={blockRaws()}>
          {(raw, i) => (
            <div
              data-block-index={i()}
              class={blockSpacingClass(
                tokens()[i() - 1],
                tokens()[i()],
                i() === 0,
              )}
              innerHTML={cachedParse(raw)}
            />
          )}
        </For>
      </div>
    </Show>
  );
}
