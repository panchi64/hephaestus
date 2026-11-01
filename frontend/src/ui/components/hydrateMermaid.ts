import { createSignal, type Accessor, type JSX, type Setter } from "solid-js";
import { insert, render } from "solid-js/web";
import { ConsoleGroup } from "./ConsoleGroup";
import { Lightbox } from "./Lightbox";
import { LoadingText } from "./LoadingText";
import { Reveal } from "./Reveal";
import { Text } from "../primitives/Text";
import {
  budgetedEmphases,
  diagramMermaidConfig,
  diagramThemeCss,
  emphasesInSource,
  observeDiagramTheme,
  readDiagramTheme,
  reserveSecondLine,
  showsLiteralBreak,
  themeSignature,
  type DiagramTheme,
} from "./mermaidTheme";

/**
 * Draw the diagrams an answer contained.
 *
 * `markdownMermaid` emits an **empty** placeholder carrying its source in a data
 * attribute, precisely so nothing is drawn at parse time; this is the pass that
 * picks those up afterwards. It is a DOM pass rather than a component for the
 * same reason `hydrateRemoteImages` is: the prose around it is parsed markdown
 * injected as HTML, so there is no JSX seam to hang a component off. It is
 * idempotent per node (a placeholder already mounted is skipped), which is what
 * makes it safe to re-run on every streaming delta.
 *
 * Three things are load-bearing here:
 *
 * **Mermaid is imported on the first diagram and never before.** It is a layout
 * engine with a parser generator behind it — larger than everything else in the
 * markdown path put together — and most threads never contain a diagram. The
 * `import("mermaid")` below is the only reference to it in the app, so the
 * bundler keeps it in a chunk of its own.
 *
 * **A diagram is a console group.** The frame is that shell, reused rather than
 * rebuilt: `plate` legend at the left of the header band, a readout at the
 * right, and the drawing set into the body. That is the one shell allowed to be
 * a box, and it is allowed because its content is a readout — which a diagram
 * is. Arrival goes through `Reveal`, opening large through `Lightbox`.
 *
 * **The SVG is sanitized before it is adopted.** Mermaid renders under
 * `securityLevel: "strict"` with `htmlLabels: false`, which is the first fence;
 * the walk in `sanitize` is the second. Model-authored source reaching a layout
 * engine that emits markup is exactly the shape that wants two.
 */

/** What a mounted diagram is currently showing. */
type DiagramState =
  | { kind: "waiting" }
  | { kind: "drawing" }
  | { kind: "drawn"; svg: SVGSVGElement; markup: string; nodes: number }
  | { kind: "unreadable" };

interface Diagram {
  host: HTMLElement;
  source: string;
  legend: string;
  /** The fence had no closer yet — the model is still writing it. */
  partial: boolean;
  state: Accessor<DiagramState>;
  setState: Setter<DiagramState>;
  /** The signature of the theme the current drawing was made with, so a theme
   *  change can be told from a re-entrant pass. */
  drawnWith: string;
  dispose: () => void;
}

/** Every mounted diagram on the page. Swept on each pass (below) rather than by
 *  a per-node observer: the pass already runs on every delta, and a `Set` walk
 *  of a handful of entries is cheaper than a `MutationObserver` per diagram. */
const live = new Set<Diagram>();

/** Rendered SVG by theme signature + source. A streaming answer re-parses its
 *  trailing block on every delta, and the block *above* the one being written
 *  keeps its node — but a theme flip re-renders all of them, and an answer
 *  reopened from history renders from scratch. Capped, oldest-first. */
const SVG_CACHE_CAP = 60;
const svgCache = new Map<string, string>();

let themeObserverInstalled = false;
let renderSeq = 0;

/* -------------------------------------------------------------------------- */
/* Sanitizing                                                                  */
/* -------------------------------------------------------------------------- */

/** Elements dropped outright from a rendered diagram.
 *
 *  `script` and the event-bearing SMIL elements are the obvious half. `image`
 *  and `foreignObject` are the less obvious half and matter just as much: an
 *  `<image href="https://…">` fetches the instant it is in the document, which
 *  would be a request leaving the operator's browser to a host named by a model
 *  that had just read a web page — the same thing `markdownLinks` refuses to let
 *  an `<img>` do. */
const DROPPED_TAGS = new Set([
  "script",
  "foreignobject",
  "iframe",
  "object",
  "embed",
  "image",
  "audio",
  "video",
  "link",
  "meta",
  "base",
  "set",
  "animate",
  "animatemotion",
  "animatetransform",
  "handler",
]);

/** Strip everything from a parsed diagram that could act rather than draw.
 *
 *  Written as a denylist rather than an element allowlist deliberately: mermaid
 *  emits a different vocabulary per diagram family and grows new ones between
 *  versions, so an allowlist would silently blank a sequence diagram the first
 *  time it learned a new shape. The things that can *execute* or *fetch*, on the
 *  other hand, are a closed and short list. */
function sanitize(svg: SVGSVGElement): void {
  for (const node of Array.from(svg.querySelectorAll("*"))) {
    if (DROPPED_TAGS.has(node.localName.toLowerCase())) {
      node.remove();
      continue;
    }
    if (node.localName.toLowerCase() === "style") {
      // Only `@import` matters here — it is the one CSS construct that fetches.
      node.textContent = (node.textContent ?? "").replace(
        /@import[^;]*;?/gi,
        "",
      );
      continue;
    }
    for (const attr of Array.from(node.attributes)) {
      const name = attr.name.toLowerCase();
      if (name.startsWith("on")) {
        node.removeAttribute(attr.name);
        continue;
      }
      // A marker reference (`url(#arrow)`) and an internal anchor are the only
      // addresses a diagram legitimately holds. Anything else leaves the page.
      if (
        (name === "href" || name === "xlink:href" || name === "src") &&
        !attr.value.trim().startsWith("#")
      )
        node.removeAttribute(attr.name);
      if (name === "style" && /url\s*\(|expression\s*\(/i.test(attr.value))
        node.removeAttribute(attr.name);
    }
  }
}

/** Let the drawing take the width it is given and keep its aspect. Mermaid sizes
 *  the root element in absolute px; inside prose capped at 72ch that overflows
 *  on anything wider than a handful of nodes. */
function makeResponsive(svg: SVGSVGElement): void {
  svg.removeAttribute("width");
  svg.removeAttribute("height");
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
  svg.style.width = "100%";
  svg.style.maxWidth = "100%";
  svg.style.height = "auto";
  svg.style.display = "block";
}

/** How many boxes the diagram drew, across the families that have boxes. Reported
 *  in the header band's readout, which is what a console group's right-hand slot
 *  is for — the frame says what this is, the readout says how big it got. */
function countNodes(svg: SVGSVGElement): number {
  return svg.querySelectorAll(
    "g.node, rect.actor-top, g.statediagram-state, g.classGroup, .er.entityBox",
  ).length;
}

/* -------------------------------------------------------------------------- */
/* Rendering                                                                   */
/* -------------------------------------------------------------------------- */

/** Mermaid's config is global and its renders share a document, so two diagrams
 *  drawn at once would race over whose theme is installed. One at a time. */
let queue: Promise<unknown> = Promise.resolve();

function enqueue<T>(job: () => Promise<T>): Promise<T> {
  const next = queue.then(job, job);
  queue = next.catch(() => undefined);
  return next;
}

async function toSvgElement(markup: string): Promise<SVGSVGElement | null> {
  const parsed = new DOMParser().parseFromString(markup, "image/svg+xml");
  const root = parsed.documentElement;
  if (!root || root.nodeName === "parsererror" || root.localName !== "svg")
    return null;
  const adopted = document.importNode(root, true) as unknown as SVGSVGElement;
  sanitize(adopted);
  makeResponsive(adopted);
  return adopted;
}

/** Render `source` once, with the reserved second line, falling back to the
 *  untransformed source when the transform did not survive. */
async function drawOnce(
  source: string,
  theme: DiagramTheme,
): Promise<SVGSVGElement | null> {
  const { default: mermaid } = await import("mermaid");
  mermaid.initialize({
    ...diagramMermaidConfig(theme),
    themeCSS: diagramThemeCss(
      theme,
      budgetedEmphases(emphasesInSource(source)),
    ),
  });

  const attempts = [reserveSecondLine(source), source];
  for (const [index, text] of attempts.entries()) {
    if (index > 0 && text === attempts[0]) break;
    try {
      const { svg } = await mermaid.render(`heph-mmd-${++renderSeq}`, text);
      const element = await toSvgElement(svg);
      if (!element) continue;
      // The transform is allowed to be wrong, but never allowed to be *visibly*
      // wrong: a diagram that rendered with the markup on screen is discarded
      // and redrawn from the source the model actually wrote.
      if (index === 0 && showsLiteralBreak(element)) continue;
      return element;
    } catch {
      // Falls through to the untransformed source, and past it to the caller's
      // quiet "not a diagram" frame.
    }
  }
  return null;
}

async function draw(diagram: Diagram): Promise<void> {
  const theme = readDiagramTheme();
  const signature = themeSignature(theme);
  // A NUL between the two halves: a diagram source can contain anything, and
  // a separator it could itself contain is a key collision waiting to happen.
  const key = `${signature}\u0000${diagram.source}`;

  const cached = svgCache.get(key);
  if (cached) {
    const element = await toSvgElement(cached);
    if (element) {
      diagram.drawnWith = signature;
      diagram.setState({
        kind: "drawn",
        svg: element,
        markup: cached,
        nodes: countNodes(element),
      });
      return;
    }
  }

  diagram.setState({ kind: "drawing" });
  const element = await enqueue(() => drawOnce(diagram.source, theme));
  if (!diagram.host.isConnected) return;
  if (!element) {
    diagram.setState({ kind: "unreadable" });
    return;
  }
  const markup = new XMLSerializer().serializeToString(element);
  svgCache.set(key, markup);
  if (svgCache.size > SVG_CACHE_CAP) {
    const oldest = svgCache.keys().next().value;
    if (oldest !== undefined && oldest !== key) svgCache.delete(oldest);
  }
  diagram.drawnWith = signature;
  diagram.setState({
    kind: "drawn",
    svg: element,
    markup,
    nodes: countNodes(element),
  });
}

/* -------------------------------------------------------------------------- */
/* The frame                                                                   */
/* -------------------------------------------------------------------------- */

/** The header band's right-hand readout. Mono, so it snaps — the machine
 *  register, which is what a count of drawn boxes is. */
function readout(state: DiagramState): JSX.Element {
  const label =
    state.kind === "drawn"
      ? `${state.nodes} ${state.nodes === 1 ? "NODE" : "NODES"}`
      : state.kind === "waiting"
        ? "RECEIVING"
        : state.kind === "drawing"
          ? "DRAWING"
          : "NOT A DIAGRAM";
  return Text({ variant: "meta", tone: "dim", children: label });
}

/** The body of the frame. A drawn diagram is wrapped in a button rather than
 *  given a separate "open" control: the drawing *is* the affordance, it gets
 *  keyboard focus and Enter for free by being the element it actually is, and
 *  the header band stays a legend and a readout. */
function body(
  state: DiagramState,
  legend: string,
  open: () => void,
): JSX.Element {
  if (state.kind === "drawn") {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "block w-full cursor-zoom-in bg-transparent p-2";
    button.setAttribute("aria-label", `View ${legend} full screen`);
    button.addEventListener("click", open);
    button.appendChild(state.svg);
    return button;
  }
  const holder = document.createElement("div");
  holder.className = "flex min-h-16 items-center justify-center p-2";
  // `insert`, not `appendChild`: a Solid component called as a function returns
  // the framework's own renderable — for `Text`, a memo rather than a `Node` —
  // and `appendChild` throws on it. `insert` is the runtime helper compiled JSX
  // uses for exactly this, and it is what makes reusing the real components
  // from a DOM pass work at all rather than re-styling a `<span>` by hand.
  insert(holder, () =>
    state.kind === "unreadable"
      ? Text({
          variant: "label",
          tone: "dim",
          children: "This diagram could not be drawn.",
        })
      : LoadingText({
          label: state.kind === "waiting" ? "Receiving" : "Drawing",
        }),
  );
  return holder;
}

function mount(diagram: Diagram): () => void {
  const [lightboxOpen, setLightboxOpen] = createSignal(false);
  const [objectUrl, setObjectUrl] = createSignal<string | undefined>();

  const release = (): void => {
    const url = objectUrl();
    if (url) URL.revokeObjectURL(url);
    setObjectUrl(undefined);
  };

  const open = (): void => {
    const state = diagram.state();
    if (state.kind !== "drawn") return;
    release();
    // Re-tagged as `image/svg+xml` and handed to an `<img>`, the way every other
    // SVG in the product is shown full screen: an `<img>` cannot execute what it
    // is pointed at, which keeps the second fence up in the one place the markup
    // leaves this document.
    setObjectUrl(
      URL.createObjectURL(new Blob([state.markup], { type: "image/svg+xml" })),
    );
    setLightboxOpen(true);
  };

  const close = (): void => {
    setLightboxOpen(false);
    release();
  };

  const dispose = render(
    () =>
      Reveal({
        motion: "fade",
        get children() {
          return [
            ConsoleGroup({
              get label() {
                return diagram.legend;
              },
              get right() {
                return readout(diagram.state());
              },
              get children() {
                return body(diagram.state(), diagram.legend, open);
              },
            }),
            Lightbox({
              get items() {
                return [{ src: objectUrl(), filename: diagram.legend }];
              },
              index: 0,
              get open() {
                return lightboxOpen();
              },
              onClose: close,
              onNavigate: () => {},
            }),
          ];
        },
      }),
    diagram.host,
  );

  return () => {
    release();
    dispose();
  };
}

/* -------------------------------------------------------------------------- */
/* The pass                                                                    */
/* -------------------------------------------------------------------------- */

function sweep(): void {
  for (const diagram of Array.from(live))
    if (!diagram.host.isConnected) {
      live.delete(diagram);
      diagram.dispose();
    }
}

/**
 * Tear down every diagram mounted under `root`.
 *
 * `sweep()` alone is not enough, and the gap is the whole reason this exists: it
 * runs at the *start of the next* `hydrateMermaid`, so a `Markdown` that unmounts
 * with nothing rendering after it — the operator leaves the thread for Settings —
 * leaves its diagrams' roots alive for the rest of the session. Each one holds a
 * `render()` scope, its signals, the detached SVG, and any lightbox object URL that
 * `release()` never got to revoke.
 *
 * So the owner disposes them, rather than the next caller inheriting the job.
 * Called from `Markdown`'s `onCleanup`, which runs while `root` still contains its
 * children — hence `contains` rather than an `isConnected` test, which is what the
 * hosts would already fail if the DOM had gone first.
 */
export function disposeMermaid(root: HTMLElement): void {
  for (const diagram of Array.from(live))
    if (!diagram.host.isConnected || root.contains(diagram.host)) {
      live.delete(diagram);
      diagram.dispose();
    }
}

/** Redraw everything on screen. The palette a diagram is drawn with is baked
 *  into its SVG as literals — it has to be, since the same markup is handed to
 *  an `<img>` in the lightbox, where no cascade reaches it — so a theme, session
 *  mode or operator-override change is a re-render rather than a repaint. */
function redrawAll(): void {
  const signature = themeSignature(readDiagramTheme());
  for (const diagram of live) {
    if (diagram.partial || diagram.drawnWith === signature) continue;
    void draw(diagram);
  }
}

/**
 * Draw every diagram placeholder under `root` that is not already mounted.
 *
 * Marks each host before doing anything asynchronous, so a re-run mid-flight
 * (the next streaming delta) does not start a second render for the same node.
 */
export function hydrateMermaid(root: HTMLElement): void {
  if (typeof document === "undefined") return;
  sweep();

  const pending = root.querySelectorAll<HTMLElement>(
    "[data-mermaid-src]:not([data-mermaid-state])",
  );
  if (pending.length === 0) return;

  if (!themeObserverInstalled) {
    themeObserverInstalled = true;
    observeDiagramTheme(redrawAll);
  }

  pending.forEach((host) => {
    host.dataset.mermaidState = "mounted";
    const partial = host.dataset.mermaidPartial !== undefined;
    const [state, setState] = createSignal<DiagramState>({
      kind: partial ? "waiting" : "drawing",
    });
    const diagram: Diagram = {
      host,
      source: host.dataset.mermaidSrc ?? "",
      legend: host.dataset.mermaidLegend?.trim() || "Diagram",
      partial,
      state,
      setState,
      drawnWith: "",
      dispose: () => {},
    };
    diagram.dispose = mount(diagram);
    live.add(diagram);
    // A fence the model has not closed yet is not a broken diagram, it is an
    // unfinished one. It gets the frame and a quiet readout, and mermaid is not
    // asked to parse it — which is what keeps a parse error from flashing on
    // screen for the whole time the model is writing.
    if (!partial) void draw(diagram);
  });
}
