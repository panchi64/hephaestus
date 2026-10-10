import type { JSX } from "solid-js";
import { cx } from "~/ui";

/** Where a branch sits in its trunk: whether it is the first row hung off it (a
 *  nested one then reaches up to its parent's socket) and the last (the trunk then
 *  turns into its elbow instead of carrying on past it). */
export interface BranchEdge {
  first: boolean;
  last: boolean;
}

/** The two trunks a row can hang off.
 *
 *  `log` is the work log's own, down from the header's chevron centre (`px-2` +
 *  half its 12px glyph = 14px). It never reaches up: the log's first row can sit
 *  inside a `Collapse`, which clips, so `WorkLog` draws the short length from the
 *  chevron to the header's foot itself.
 *
 *  `nested` hangs a script's calls off the script's own socket — 8px in (half the
 *  16px socket) — and its first branch reaches the 4px (the row's `py-1`) up to
 *  that socket's foot. */
const GEOMETRY = {
  log: { trunk: "left-3.5", elbow: "w-3.5", body: "pl-7", reach: "" },
  nested: { trunk: "left-2", elbow: "w-3", body: "pl-5", reach: "-top-1" },
} as const;

type Geometry = (typeof GEOMETRY)[keyof typeof GEOMETRY];

/** The trunk's vertical extent: from the parent when first (and it reaches), down
 *  past the row when it is not last, else to the elbow — 12px, plus the 4px reach
 *  when there is one. */
function trunkSpan(g: Geometry, edge: BranchEdge): string {
  const reaches = edge.first && g.reach !== "";
  const top = reaches ? g.reach : "top-0";
  if (!edge.last) return `${top} bottom-0`;
  return `${top} ${reaches ? "h-4" : "h-3"}`;
}

/** One row hung off a trunk by an elbow — the work log drawn as a schematic, every
 *  step a trace landing on a pin, the whole run one connected line (§8, §9).
 *
 *  Each branch draws its own length of trunk rather than the log drawing one line
 *  behind all of them, because only a row knows its own height: a row that opens
 *  grows, and a trunk measured once would stop short of the rows beneath it. The
 *  last branch stops its trunk at its elbow, which is what turns `├` into `└`.
 *
 *  The elbow lands at 12px — the centre of a schematic `ProcessRow` (`py-1` plus
 *  half the 16px socket), so it meets the glyph's socket edge-on. A row that is
 *  not a `ProcessRow` (a host terminal) takes the elbow on its own edge instead.
 *
 *  `lit` marks the running row: its length of trunk and its elbow turn `info`.
 *  `chassis` dashes the elbow — the trace for a row the chassis put
 *  there (injected context, a review) rather than one the model did, which the
 *  row's own dashed socket repeats; bare rows no longer have a card to refuse, so
 *  this is what tells the two apart at a glance. `trace` draws the strokes in
 *  (`.ody-trace-*`) — the collapsed log's latest step, as it arrives. */
export function Branch(props: {
  edge: BranchEdge;
  variant?: keyof typeof GEOMETRY;
  lit?: boolean;
  chassis?: boolean;
  trace?: boolean;
  children: JSX.Element;
}): JSX.Element {
  const g = (): Geometry => GEOMETRY[props.variant ?? "log"];
  return (
    <div class={cx("relative", g().body)}>
      <span
        aria-hidden="true"
        class={cx(
          "pointer-events-none absolute flex",
          g().trunk,
          trunkSpan(g(), props.edge),
          props.trace && "ody-trace-down",
        )}
      >
        <span
          class={cx(
            "border-l transition-colors",
            props.lit ? "border-info" : "border-line",
          )}
        />
      </span>
      <span
        aria-hidden="true"
        class={cx(
          "pointer-events-none absolute top-3 h-0 border-t transition-colors",
          g().trunk,
          g().elbow,
          props.lit ? "border-info" : "border-line",
          props.chassis && "border-dashed",
          props.trace && "ody-trace-across",
        )}
      />
      {props.children}
    </div>
  );
}
