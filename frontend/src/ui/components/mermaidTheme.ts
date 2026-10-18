import type { MermaidConfig } from "mermaid";
import {
  contrastRatio,
  normalizeHex,
  relativeLuminance,
} from "../theme/contrast";

/**
 * The design system, expressed as something mermaid can be configured with.
 *
 * Mermaid takes **literal colours** — `themeVariables` has no idea what a custom
 * property is, and `themeCSS` is serialised into the generated `<svg>`, which is
 * a subtree the cascade reaches but which must still carry its own values for the
 * lightbox (where the same markup is handed to an `<img>` and stops inheriting
 * anything at all). Hephaestus has two themes and per-theme operator accent overrides on
 * top of them, so a hardcoded palette would be wrong two different ways.
 *
 * So the values are *read back out of the cascade* — `getComputedStyle` on
 * `<html>`, which is where every one of those layers has already resolved —
 * and re-emitted as literals. Nothing in here declares a colour of its own; the
 * fallbacks exist only for the case where a property is missing entirely, and
 * they are the Ink shipped values.
 *
 * Four things stock mermaid does that the system forbids, and what is done here
 * instead:
 *
 *   1. **Colour.** Its palette is decorative; a screen at rest is grayscale. The
 *      diagram is built from `surface`/`text` and spends accents only on the one
 *      focused node and on severity — at most two hues, enforced in
 *      `diagramThemeCss`.
 *   2. **Borders.** It strokes every node. Space, then surface value, then the
 *      shadow's ring; a real border is the last resort and a node is not that
 *      case. Nodes are `surface-raised` fills with no stroke at all.
 *   3. **The two voices.** It sets labels in sans. A node in an architecture
 *      diagram is an identifier naming a region — that is `plate`, and an edge
 *      label reporting a condition is `meta`.
 *   4. **Severity by hue.** Caution is a *tint* and alert is a *solid fill*, so
 *      the two separate on luminance and survive any colour vision. The label on
 *      a filled node is chosen by measurement (`legibleOn`), not assumed, because
 *      white on the shipped alert red measures 3.0:1 and black on it measures
 *      6.9:1 — and on Paper that verdict reverses.
 */

/** Ink's shipped values, used only when a property is missing or unparseable. */
const FALLBACK = {
  bg: "#0a0a0a",
  page: "#000000",
  node: "#161616",
  line: "#212121",
  lineStrong: "#333333",
  edge: "#6e6e6e",
  edgeText: "#a8a8a8",
  nodeText: "#ffffff",
  accent: "#34d67f",
  warn: "#f2a93b",
  alert: "#ff5c5c",
} as const;

const MONO_STACK = `"JetBrains Mono", ui-monospace, "SFMono-Regular", monospace`;

/** The resolved palette one diagram is drawn with. Every field is a literal
 *  `#rrggbb`, already through the theme and any operator override. */
export interface DiagramTheme {
  /** The ground a diagram sits on — the console group's body. */
  bg: string;
  /** The page behind that, for a label that has to punch out of a filled node. */
  page: string;
  /** Node fill. */
  node: string;
  /** Node label. */
  nodeText: string;
  /** Edge stroke and arrowhead. Content, not a border — see `EDGE_WIDTH`. */
  edge: string;
  /** Edge label. */
  edgeText: string;
  /** Hairline rules: a subgraph's boundary. */
  line: string;
  /** The emphasized neutral fill a demoted accent falls back to. */
  lineStrong: string;
  /** The signature accent. */
  accent: string;
  warn: string;
  alert: string;
  /** Whether this theme reads as dark, by measurement rather than by name — an
   *  operator can point `data-theme` wherever they like, and mermaid derives a
   *  few of its own values from this flag. */
  dark: boolean;
}

/**
 * Edges are **1.5px `text-dim`**, and deliberately not the `line` token.
 *
 * `line` is a hairline *border* — the rule between two rows, the edge of a
 * frame. An edge in a diagram is not a border: it is the content, the thing the
 * diagram is *about*. Drawn at `line` it disappeared into the group's own frame
 * and the arrowheads stopped being legible at all, which is the defect this
 * comment exists to keep fixed. `text-dim` clears 3:1 against both the body
 * ground and a node fill in both themes (pinned in `contrast.test.ts`).
 */
export const EDGE_WIDTH = "1.5px";

/** The token pairs a diagram's legibility rests on, named so `contrast.test.ts`
 *  can check them against what tokens.css actually declares, in both themes.
 *
 *  The floors are split on purpose: an edge is a graphical object and takes
 *  WCAG 1.4.11's 3:1, a label is text and takes 1.4.3's 4.5:1. */
export interface DiagramContrastPair {
  /** What is being measured, for the failure message. */
  what: string;
  /** Foreground token name, without the leading `--`. */
  fg: string;
  /** Background token name, without the leading `--`. */
  bg: string;
  floor: number;
}

export const DIAGRAM_CONTRAST_PAIRS: readonly DiagramContrastPair[] = [
  { what: "edge over the group body", fg: "text-dim", bg: "surface", floor: 3 },
  {
    what: "edge crossing a node",
    fg: "text-dim",
    bg: "surface-raised",
    floor: 3,
  },
  {
    what: "node label",
    fg: "text-bright",
    bg: "surface-raised",
    floor: 4.5,
  },
  { what: "edge label", fg: "text", bg: "surface", floor: 4.5 },
  {
    what: "label on a demoted-severity node",
    fg: "text-bright",
    bg: "line-strong",
    floor: 4.5,
  },
];

/** How much of the severity hue a *caution* tint carries. Low enough that the
 *  cell still reads as a node rather than as a filled warning — the fill weight
 *  is the signal, and alert is the solid one. */
export const CAUTION_TINT = 0.22;

/** One sRGB channel, linearized (WCAG's transfer function, same as `contrast`). */
function toLinear(channel: number): number {
  const c = channel / 255;
  return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
}

function toSrgb(linear: number): number {
  const c =
    linear <= 0.0031308
      ? linear * 12.92
      : 1.055 * Math.pow(linear, 1 / 2.4) - 0.055;
  return Math.max(0, Math.min(255, Math.round(c * 255)));
}

function channels(hex: string): [number, number, number] | null {
  const n = normalizeHex(hex);
  if (!n) return null;
  return [
    parseInt(n.slice(1, 3), 16),
    parseInt(n.slice(3, 5), 16),
    parseInt(n.slice(5, 7), 16),
  ];
}

/**
 * Mix two colours, **in linear light**, and return the `#rrggbb` result.
 *
 * Done here rather than with `color-mix()` because mermaid needs a literal: a
 * `color-mix(…)` string in `themeVariables` reaches the serialised SVG as text
 * the lightbox's `<img>` has no cascade to resolve it against. Linear rather
 * than a naive channel average, because averaging gamma-encoded values darkens
 * a tint noticeably — mixing 22% amber into near-black the naive way lands
 * somewhere visibly muddier than the same mix on white, and the two themes then
 * stop matching each other.
 *
 * `weight` is how much of `a` survives, 0–1. Returns `b` if either input is not
 * a colour, since the caller's fallback is always the surface it was tinting.
 */
export function mixHex(a: string, b: string, weight: number): string {
  const ca = channels(a);
  const cb = channels(b);
  if (!ca || !cb) return normalizeHex(b) ?? FALLBACK.node;
  const w = Math.max(0, Math.min(1, weight));
  const out = ca.map((channel, i) =>
    toSrgb(toLinear(channel) * w + toLinear(cb[i]) * (1 - w)),
  );
  return `#${out.map((v) => v.toString(16).padStart(2, "0")).join("")}`;
}

/**
 * The most legible of `candidates` on `fill`.
 *
 * A filled severity node cannot assume white text. The shipped Ink alert red
 * measures 3.0:1 against white and 6.9:1 against black; on Paper the alert red
 * measures 5.6:1 against white and 3.8:1 against black. The right answer flips
 * between the two themes, and an operator override can flip it again — so it is
 * measured rather than chosen, every time the theme is read.
 */
export function legibleOn(fill: string, candidates: readonly string[]): string {
  let best = candidates[0];
  let bestRatio = -1;
  for (const candidate of candidates) {
    const ratio = contrastRatio(candidate, fill) ?? 0;
    if (ratio > bestRatio) {
      bestRatio = ratio;
      best = candidate;
    }
  }
  return best;
}

function readHex(
  styles: CSSStyleDeclaration,
  property: string,
  fallback: string,
): string {
  return normalizeHex(styles.getPropertyValue(property).trim()) ?? fallback;
}

/**
 * Read the live palette off `<html>`.
 *
 * `<html>` specifically, and not the diagram's own container: `data-theme` and
 * lives on the root element, the operator's override sheet is written at
 * `html[data-theme]`, and the root is therefore the one node where both layers have
 * already resolved into a single value per token.
 */
export function readDiagramTheme(): DiagramTheme {
  if (typeof document === "undefined")
    return {
      bg: FALLBACK.bg,
      page: FALLBACK.page,
      node: FALLBACK.node,
      nodeText: FALLBACK.nodeText,
      edge: FALLBACK.edge,
      edgeText: FALLBACK.edgeText,
      line: FALLBACK.line,
      lineStrong: FALLBACK.lineStrong,
      accent: FALLBACK.accent,
      warn: FALLBACK.warn,
      alert: FALLBACK.alert,
      dark: true,
    };
  const styles = getComputedStyle(document.documentElement);
  const page = readHex(styles, "--bg", FALLBACK.page);
  return {
    bg: readHex(styles, "--surface", FALLBACK.bg),
    page,
    node: readHex(styles, "--surface-raised", FALLBACK.node),
    nodeText: readHex(styles, "--text-bright", FALLBACK.nodeText),
    edge: readHex(styles, "--text-dim", FALLBACK.edge),
    edgeText: readHex(styles, "--text", FALLBACK.edgeText),
    line: readHex(styles, "--line", FALLBACK.line),
    lineStrong: readHex(styles, "--line-strong", FALLBACK.lineStrong),
    accent: readHex(styles, "--accent", FALLBACK.accent),
    warn: readHex(styles, "--accent-warn", FALLBACK.warn),
    alert: readHex(styles, "--accent-alert", FALLBACK.alert),
    dark: (relativeLuminance(page) ?? 0) < 0.5,
  };
}

/** A value that changes whenever anything a diagram is drawn with changes, so a
 *  rendered SVG can be cached against it and thrown away on a theme, mode or
 *  override change without comparing eleven fields by hand. */
export function themeSignature(theme: DiagramTheme): string {
  return [
    theme.bg,
    theme.page,
    theme.node,
    theme.nodeText,
    theme.edge,
    theme.edgeText,
    theme.line,
    theme.lineStrong,
    theme.accent,
    theme.warn,
    theme.alert,
    theme.dark ? "dark" : "light",
  ].join("/");
}

/**
 * The three emphases a node may carry, in the order they outrank each other.
 *
 * `alert` and `caution` are the severity ladder — a solid fill and a tint of the
 * weight relationship §10.15 gives caution and warning. `focus` is the one node
 * the diagram is *about*, and it is last precisely because it is the one that
 * gets dropped when a diagram would otherwise show three accent hues at once.
 */
export const DIAGRAM_EMPHASES = ["alert", "caution", "focus"] as const;
export type DiagramEmphasis = (typeof DIAGRAM_EMPHASES)[number];

/** Whichever emphases in `present` fit inside the two-accent budget, by the
 *  ranking above. Anything over the budget still renders emphasized — it falls
 *  back to `line-strong`, which separates on fill weight rather than on hue, so
 *  nothing is silently flattened back to an ordinary node. */
export function budgetedEmphases(
  present: Iterable<string>,
): Set<DiagramEmphasis> {
  const wanted = new Set(present);
  const kept = new Set<DiagramEmphasis>();
  for (const emphasis of DIAGRAM_EMPHASES) {
    if (kept.size >= 2) break;
    if (wanted.has(emphasis)) kept.add(emphasis);
  }
  return kept;
}

/**
 * Which class names in a mermaid source ask for an emphasis.
 *
 * Both of the syntaxes that attach a class are recognised — the inline
 * `A["…"]:::alert` form and the standalone `class A,B alert` statement — because
 * the budget above has to be decided *before* the diagram renders, and the
 * rendered SVG is too late to change which hues it spends.
 */
export function emphasesInSource(source: string): Set<string> {
  const found = new Set<string>();
  for (const match of source.matchAll(/:::\s*([A-Za-z_][\w-]*)/g))
    found.add(match[1]);
  for (const match of source.matchAll(
    /^[ \t]*class[ \t]+[^\n]*?[ \t]+([A-Za-z_][\w-]*)[ \t]*$/gm,
  ))
    found.add(match[1]);
  return found;
}

/** Mermaid's own config, minus the theme values (see `mermaidThemeVariables`). */
export function diagramMermaidConfig(theme: DiagramTheme): MermaidConfig {
  return {
    startOnLoad: false,
    // The model authors the source. `strict` runs every label through mermaid's
    // own sanitizer and refuses click bindings; `htmlLabels: false` keeps labels
    // as SVG `<text>` rather than a `<foreignObject>` full of HTML, which is the
    // construct that would carry markup out of a label and into the document.
    // `hydrateMermaid` sanitizes the result a second time regardless — this is
    // the first of the two fences, not the only one.
    securityLevel: "strict",
    // A parse failure must never paint. Mermaid's built-in error diagram is a
    // cartoon bomb appended to the page; the caller renders its own quiet
    // fallback instead.
    suppressErrorRendering: true,
    theme: "base",
    darkMode: theme.dark,
    fontFamily: MONO_STACK,
    // Set at the TOP level as well as per-diagram, and both are needed: the
    // flowchart renderer reads its own key, and several of the other families
    // read only this one. With just the per-diagram key mermaid still emitted
    // `<foreignObject>` labels — a block of HTML inside the SVG, which is both
    // the construct `securityLevel` is least able to vouch for and markup the
    // strict XML parse on the way back in rejects outright.
    htmlLabels: false,
    themeVariables: mermaidThemeVariables(theme),
    flowchart: {
      htmlLabels: false,
      useMaxWidth: true,
      // Straight segments. The system's register is engineered, and a splined
      // edge reads as decoration in a diagram whose whole job is to be exact.
      curve: "linear",
      padding: 10,
      nodeSpacing: 44,
      rankSpacing: 52,
    },
    sequence: { useMaxWidth: true, mirrorActors: false },
    er: { useMaxWidth: true },
    class: { useMaxWidth: true },
    state: { useMaxWidth: true },
    gantt: { useMaxWidth: true },
  };
}

/** `themeVariables` for mermaid's `base` theme, covering the diagram families a
 *  model actually reaches for. Everything resolves to one of five tokens; the
 *  repetition is mermaid's API surface, not five decisions. */
export function mermaidThemeVariables(
  theme: DiagramTheme,
): Record<string, string> {
  return {
    darkMode: String(theme.dark),
    background: theme.bg,
    fontFamily: MONO_STACK,
    fontSize: "10px",

    primaryColor: theme.node,
    primaryBorderColor: theme.node,
    primaryTextColor: theme.nodeText,
    secondaryColor: theme.node,
    secondaryBorderColor: theme.node,
    secondaryTextColor: theme.nodeText,
    tertiaryColor: theme.bg,
    tertiaryBorderColor: theme.line,
    tertiaryTextColor: theme.edgeText,

    mainBkg: theme.node,
    nodeBorder: theme.node,
    nodeTextColor: theme.nodeText,
    textColor: theme.edgeText,
    titleColor: theme.nodeText,
    labelColor: theme.nodeText,

    lineColor: theme.edge,
    edgeLabelBackground: theme.bg,

    clusterBkg: theme.bg,
    clusterBorder: theme.line,

    // Sequence diagrams.
    actorBkg: theme.node,
    actorBorder: theme.node,
    actorTextColor: theme.nodeText,
    actorLineColor: theme.edge,
    signalColor: theme.edge,
    signalTextColor: theme.edgeText,
    labelBoxBkgColor: theme.node,
    labelBoxBorderColor: theme.node,
    labelTextColor: theme.nodeText,
    loopTextColor: theme.edgeText,
    noteBkgColor: theme.node,
    noteBorderColor: theme.node,
    noteTextColor: theme.nodeText,
    sequenceNumberColor: theme.page,

    // State / class diagrams.
    transitionColor: theme.edge,
    transitionLabelColor: theme.edgeText,
    stateBkg: theme.node,
    altBackground: theme.bg,
    compositeBackground: theme.bg,
    compositeTitleBackground: theme.bg,
    compositeBorder: theme.line,
    classText: theme.nodeText,

    // Mermaid still wants an error palette even with its error diagram
    // suppressed; point it at the tokens rather than at its own pink.
    errorBkgColor: theme.node,
    errorTextColor: theme.edgeText,
  };
}

/**
 * The stylesheet mermaid serialises into the `<svg>` it generates.
 *
 * This is where the four violations are actually closed — `themeVariables` can
 * say what colour a node is, but only CSS can say that it has no stroke, that
 * its label is uppercase mono at `plate`'s tracking, and that an edge is 1.5px.
 *
 * `emphases` is the budget from `budgetedEmphases`: a class inside it gets its
 * hue, and a class outside it gets `line-strong`. Both are fills, so a node the
 * budget demoted is still visibly emphasized — it just stops spending a third
 * accent to say so.
 */
export function diagramThemeCss(
  theme: DiagramTheme,
  emphases: ReadonlySet<DiagramEmphasis>,
): string {
  const caution = mixHex(theme.warn, theme.node, CAUTION_TINT);
  const onAlert = legibleOn(theme.alert, [theme.page, theme.nodeText]);
  const onAccent = legibleOn(theme.accent, [theme.page, theme.nodeText]);
  const neutral = theme.lineStrong;
  const onNeutral = legibleOn(neutral, [theme.page, theme.nodeText]);

  const emphasis = (
    name: DiagramEmphasis,
    fill: string,
    label: string,
  ): string => {
    const budgeted = emphases.has(name);
    const bg = budgeted ? fill : neutral;
    const fg = budgeted ? label : onNeutral;
    return `
.node.${name} rect,
.node.${name} circle,
.node.${name} ellipse,
.node.${name} polygon,
.node.${name} path { fill: ${bg} !important; stroke: none !important; }
.node.${name} .nodeLabel,
.node.${name} .nodeLabel *,
.node.${name} text { fill: ${fg} !important; }`;
  };

  return `
/* EVERY declaration in here is !important, and that is a decision rather than
   sloppiness. Mermaid styles its output three ways at once — a stylesheet it
   serialises into the <svg>, presentation attributes, and inline style
   attributes on individual elements — and the inline ones win against any
   selector that can be written. This sheet is not participating in a cascade
   with a peer; it is a system overriding a foreign widget wholesale, and a rule
   here that loses silently is a node rendered in mermaid's palette. */

/* A node is a surface, not a box: no stroke at any size, and no drop shadow.
   Mermaid v12 hangs a light-grey drop-shadow filter off every node — decoration
   in a system where elevation is surface value and the shadow's own ring.

   Every selector here ends in an ELEMENT, never in one of mermaid's own class
   names (.basic.label-container and friends). With everything !important the
   winner is decided on specificity, and a three-class selector in this block
   would quietly outrank the two-class severity rules at the bottom — which is
   exactly how an alert node renders in the ordinary node fill. */
.node rect,
.node circle,
.node ellipse,
.node polygon,
.node path,
rect.actor,
.statediagram-state rect,
.classGroup rect,
.er.entityBox,
.note rect {
  fill: ${theme.node} !important;
  stroke: none !important;
  filter: none !important;
}

/* Node labels are identifiers naming a region -- the plate step: 10px mono,
   500, uppercase, +0.16em. */
.nodeLabel,
.nodeLabel *,
.node text,
.node text tspan,
text.actor,
text.actor tspan,
.classTitle,
.statediagram-state text,
.er.entityLabel,
.noteText,
.noteText tspan {
  font-family: ${MONO_STACK} !important;
  font-size: 10px !important;
  font-weight: 500 !important;
  letter-spacing: 0.16em !important;
  text-transform: uppercase !important;
  fill: ${theme.nodeText} !important;
}

/* An edge label reports a condition -- the meta step: 11px mono, 500,
   uppercase, +0.08em, and at the text token rather than text-dim because it is
   the one piece of type in here small enough to need the headroom. */
.edgeLabel,
.edgeLabel *,
.messageText,
.messageText tspan,
.loopText,
.loopText tspan,
.cluster text,
.cluster text tspan {
  font-family: ${MONO_STACK} !important;
  font-size: 11px !important;
  font-weight: 500 !important;
  letter-spacing: 0.08em !important;
  text-transform: uppercase !important;
  fill: ${theme.edgeText} !important;
  color: ${theme.edgeText} !important;
}
/* The plate behind an edge label, so an edge passing under it does not read
   through the type. It is the group's own ground, not a third surface. */
.edgeLabel rect,
.edgeLabel .label-container,
.labelBkg,
.edgeLabel .background {
  fill: ${theme.bg} !important;
  stroke: none !important;
}

/* Edges are content. 1.5px text-dim, solid arrowheads. */
.edgePath .path,
.edgePath path,
.flowchart-link,
.messageLine0,
.messageLine1,
.transition,
.relation,
.actor-line,
.edge-thickness-normal,
.edge-thickness-thick {
  stroke: ${theme.edge} !important;
  stroke-width: ${EDGE_WIDTH} !important;
  fill: none !important;
  filter: none !important;
}
.edgePath .arrowheadPath,
.arrowheadPath,
.marker,
.marker path,
marker path,
marker polygon,
marker circle {
  fill: ${theme.edge} !important;
  stroke: ${theme.edge} !important;
  stroke-width: 1px !important;
}

/* A subgraph is a region, so it takes the hairline a region takes. */
.cluster rect,
.cluster polygon {
  fill: ${theme.bg} !important;
  stroke: ${theme.line} !important;
  stroke-width: 1px !important;
  filter: none !important;
}
.cluster text,
.cluster text tspan,
.cluster .nodeLabel { fill: ${theme.edge} !important; }
${emphasis("alert", theme.alert, onAlert)}
${emphasis("caution", caution, theme.nodeText)}
${emphasis("focus", theme.accent, onAccent)}
`;
}

/* -------------------------------------------------------------------------- */
/* The reserved second line                                                   */
/* -------------------------------------------------------------------------- */

/** Lines that are statements about the diagram rather than nodes in it. */
const NOT_A_NODE_LINE =
  /^[ \t]*(?:%%|---|classDef\b|class\b|style\b|linkStyle\b|click\b|direction\b|subgraph\b|end\b|graph\b|flowchart\b)/;

/** `id` + an opening bracket + a label + the matching closer. The two-character
 *  bracket forms are listed before the one-character ones in both alternations so
 *  `A[[Sub]]` pairs `[[` with `]]` rather than `[` with `]`. The id may not end
 *  in `-`, or `A-->B[x]` would match with `A--` as the id and `B[x` as the
 *  label. A quoted label is tried first, so a label containing `>` or `|`
 *  survives. */
const NODE_LABEL =
  /\b([A-Za-z0-9_](?:[\w-]*[A-Za-z0-9_])?)(\[\[|\(\(|\[\(|\[\/|\[\\|\{\{|\[|\(|\{|>)("(?:[^"\\]|\\.)*"|[^\])}>|\n]*?)(\]\]|\)\)|\)\]|\/\]|\\\]|\}\}|\]|\)|\})/g;

/** The character that holds the reserved line open. A no-break space, not an
 *  entity: with `htmlLabels: false` a label is SVG `<text>`, and `&nbsp;` there
 *  is six visible characters rather than one invisible one. */
const RESERVED_LINE = " ";

/**
 * Give every node a second line whether or not it uses one.
 *
 * A caution-and-warning cell's rule, applied to a diagram: a cell is the legend and
 * the reason it is lit, and the second line is reserved either way — so a node
 * changing state changes its *contents* and never its size. Without this, a node
 * that gains a severity reason grows by a line and shoves the whole layout
 * sideways, which is the one thing a diagram redrawn mid-run must not do.
 *
 * Only flowcharts are touched. The other families size their boxes from a
 * grammar that has nowhere to put a spare line, and guessing at one would mean
 * editing a sequence diagram's participant list.
 *
 * The caller renders this and **falls back to the original source** if it does
 * not survive — see `drawOnce`. A transform over model-authored text has to be
 * allowed to be wrong.
 */
export function reserveSecondLine(source: string): string {
  const firstLine = source
    .split("\n")
    .map((l) => l.trim())
    .find((l) => l && !l.startsWith("%%"));
  if (!firstLine || !/^(?:flowchart|graph)\b/.test(firstLine)) return source;

  return source
    .split("\n")
    .map((line) => {
      if (NOT_A_NODE_LINE.test(line)) return line;
      return line.replace(
        NODE_LABEL,
        (whole, id: string, open: string, label: string, close: string) => {
          const quoted = label.startsWith('"') && label.endsWith('"');
          const inner = quoted ? label.slice(1, -1) : label;
          if (!inner.trim()) return whole;
          if (inner.includes("<br")) return whole;
          // An unquoted label carrying a quote cannot be re-quoted without
          // changing what it says, so it keeps its single line.
          if (!quoted && inner.includes('"')) return whole;
          return `${id}${open}"${inner}<br/>${RESERVED_LINE}"${close}`;
        },
      );
    })
    .join("\n");
}

/** Whether a rendered diagram shows a literal `<br` — the tell that the line
 *  break was taken as text rather than as a break, which would put the markup on
 *  screen instead of reserving a line. Cheaper and far more decisive than
 *  guessing at mermaid's label grammar from the outside. */
export function showsLiteralBreak(svg: SVGSVGElement): boolean {
  return (svg.textContent ?? "").includes("<br");
}

/**
 * Call `onChange` whenever the palette a diagram is drawn with could have moved.
 *
 * `data-theme` (Ink/Paper) is an attribute on `<html>`, and the operator's override
 * sheet is keyed on it, so one observer on the root's attributes covers both layers.
 *
 * Returns the disconnect function.
 */
export function observeDiagramTheme(onChange: () => void): () => void {
  if (
    typeof MutationObserver === "undefined" ||
    typeof document === "undefined"
  )
    return () => {};
  const observer = new MutationObserver(onChange);
  observer.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme"],
  });
  return () => observer.disconnect();
}
