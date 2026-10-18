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
import { markedCode } from "./markdownCode";
import { markedLinks } from "./markdownLinks";
import { markedMath } from "./markdownMath";
import { markedMermaid } from "./markdownMermaid";
import { hydrateLocalImages, type LocalImageResolver } from "./localImages";
import { hydrateRemoteImages } from "./remoteImages";
import { disposeMermaid, hydrateMermaid } from "./hydrateMermaid";

export interface MarkdownProps {
  /** Markdown source. Rendered with token-styled prose (.heph-prose). */
  children: string;
  class?: string;
  /** Add a hover copy button to each rendered code block. Default true. */
  copyCode?: boolean;
  /** Render and cache each top-level block separately, so an unchanged prefix keeps
   *  its DOM across streaming deltas instead of the whole body re-parsing, which is
   *  what flickers KaTeX mid-stream. Default false. */
  streamStable?: boolean;
  /** Headings one step down the reading scale (`.heph-prose-dense`), for a document
   *  read in a narrow pane where full-size headings outshout the pane's own title.
   *  Body text keeps the reading size. Default false. */
  dense?: boolean;
  /** Resolves `![…](screenshot:<id>)`. Without it such an image shows its caption. */
  resolveLocalImage?: LocalImageResolver;
}

marked.setOptions({ gfm: true, breaks: true });
marked.use(markedMath);
marked.use(markedMermaid);
marked.use(markedLinks);
marked.use(...markedCode);

/** The `raw` of each top-level token, in document order: the blocks the
 *  `streamStable` path renders. For callers that map a line to a block, such as a
 *  passage anchor. */
export function markdownBlocks(source: string): string[] {
  return marked.lexer(source ?? "").map((t) => t.raw);
}

/** Block raw source → parsed HTML, shared by every `streamStable` instance. Evicts
 *  oldest-first (Map insertion order) past the cap. */
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

/** Vertical rhythm between `streamStable` block wrappers. The wrappers, not the
 *  `<p>`/`<h*>` inside them, are `.heph-prose`'s direct children, so theme.css's
 *  heading-aware rules never reach them. Every value is transcribed from theme.css:
 *  change both together, or an answer spaces differently streaming than reloaded. */
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

/** The copy button for a `pre`, built as a detached node rather than innerHTML; its
 *  click is handled by delegation. */
function makeCopyButton(): HTMLButtonElement {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.dataset.codeCopy = "";
  btn.setAttribute("aria-label", "Copy code");
  btn.className =
    "heph-code-copy absolute right-1 top-1 hidden border border-line bg-raised px-2 py-0.5 text-micro uppercase tracking-label text-dim transition-colors hover:text-bright group-hover/code:block focus:block focus:outline-none";
  btn.textContent = "Copy";
  return btn;
}

/**
 * Renders markdown as token-styled prose, for assistant replies, reports and
 * document bodies. Each code block gets a hover button that copies its source.
 *
 * The source is model- or user-authored and lands as HTML. `markdownLinks` closes
 * the three ways markup or a URL could get out of it: hrefs are scheme-checked and
 * escaped, images degrade to alt text, and raw HTML is escaped to text. Everything
 * else is markup `marked` wrote from tokens, or highlight.js output inside a fence
 * (`markdownCode`: escaped source in class-only spans), so there is nothing for a
 * sanitizer to remove. A mermaid fence is the exception: a layout engine writes it,
 * and `hydrateMermaid` sanitizes it twice and never `innerHTML`s it.
 *
 * A `data-open-path` path carries no authority: the click only relays it, and the
 * backend decides whether it names a file in the operator's projects.
 */
export function Markdown(props: MarkdownProps): JSX.Element {
  const [local] = splitProps(props, [
    "children",
    "class",
    "copyCode",
    "streamStable",
    "dense",
    "resolveLocalImage",
  ]);
  const proseClass = (): string =>
    cx("heph-prose", local.dense && "heph-prose-dense", local.class);
  // Gated on the render path: memos are eager, so ungated this would re-parse the
  // whole answer on every streaming delta for output the block path never uses.
  const html = createMemo(() =>
    local.streamStable
      ? ""
      : (marked.parse(local.children ?? "", { async: false }) as string),
  );
  const tokens = createMemo(() =>
    local.streamStable ? marked.lexer(local.children ?? "") : [],
  );
  // `<For>` iterates raw strings because `marked.lexer` mints new token objects per
  // call. An unchanged block's raw stays `===`, so its DOM (and KaTeX) is kept and
  // only the trailing block re-renders.
  const blockRaws = createMemo(() => tokens().map((t) => t.raw));

  let ref: HTMLDivElement | undefined;

  // Post-render pass, re-run on every re-parse and idempotent per node (one dataset
  // read per `pre`/`table`): wraps each `pre` in a copy-button host and each `table`
  // in a scroll host.
  const enhance = (): void => {
    if (!ref) return;
    // Ahead of the `copyCode` gate because this is content: a remote image has no
    // `src` until it runs (markdownLinks defers the load), and a diagram is drawn
    // only here (`markdownMermaid` emits a placeholder to keep the parse synchronous).
    hydrateRemoteImages(ref);
    hydrateLocalImages(ref, local.resolveLocalImage);
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
    // Re-runs when `copyCode` is toggled, so blocks rendered while it was off get
    // their buttons.
    void local.copyCode;
    // Track re-parses (streaming deltas) so new blocks get enhanced.
    if (local.streamStable) blockRaws();
    else html();
    // Scheduled even with `copyCode` off, since the pass also loads remote images.
    queueMicrotask(enhance);
  });

  // One delegated handler on the container for copy buttons and open-path links,
  // so controls that arrive mid-stream are covered.
  const onClick = (e: MouseEvent): void => {
    const target = e.target as HTMLElement;
    const opener = target.closest<HTMLElement>("[data-open-path]");
    if (opener) {
      // `openHostPath` reports its own failures.
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
    // `hydrateMermaid` reaps a diagram's detached render root only on its next pass,
    // which may never come (leaving a thread for Settings renders no more markdown).
    if (ref) disposeMermaid(ref);
  });

  // Two elements, not one: Solid's `innerHTML` prop can't coexist with rendered
  // children on the same node.
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
