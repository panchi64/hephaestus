import { Show, type JSX } from "solid-js";
import { pct } from "~/lib/format";
import { RevealPopover, StatusCell } from "~/ui";
import type { ContextUsage, LastRequest } from "../model";
import { ContextBreakdown } from "./ContextBreakdown";
import { foldMarker } from "./contextRows";

export interface ContextBarProps {
  /** The backend-derived context-window state. Non-null by construction: the caller
   *  mounts the bar only once a run has reported one, because a gauge with nothing to
   *  measure has nothing to say. The window being genuinely unknown is the send gate's
   *  to report, not this one's. */
  usage: ContextUsage;
  /** The last model request's own figures, for the panel — the route it took and what
   *  the provider's cache did with it. Null on a thread that has produced no response.
   *  The bar itself never renders this: it is a second measurement of the same request
   *  the split describes, and it belongs where the operator is already reading one. */
  lastRequest?: LastRequest | null;
}

/** The backend's severity, as the figure's tone.
 *
 *  `nominal` deliberately does **not** become the nominal green. A gauge at rest has
 *  no verdict to deliver — the window is filling, which is what windows do — and a
 *  green figure spends attention saying so. It also devalues the colour that matters:
 *  if the figure is always coloured, a colour change is no longer a signal. The text
 *  tone until the operator's warn boundary, then amber, then red, means the only time
 *  it catches the eye is the time it should.
 *
 *  The thresholds themselves stay the backend's — this maps a level, it doesn't
 *  decide one. */
const FIGURE_TONE = {
  // The cell's own tone, so the figure recedes with the strip it sits in.
  nominal: "",
  warn: "text-warn",
  alert: "text-alert",
} as const;

/** How full the model's context window is, as the last cell of the composer's status
 *  strip: a 64px track and the figure.
 *
 *  It is **the one readout left on the bar rather than behind Stats**. Everything in
 *  the stats panel is a *tally* — what the thread has spent, counting up with no
 *  ceiling in sight. This is the one figure with a ceiling, and the ceiling is the
 *  point: the operator is not tracking how many tokens the window holds, they are
 *  watching for the moment it runs out. The track makes that a length read at a glance;
 *  the figure beside it is there for the moment a glance isn't enough.
 *
 *  **The warn tick is where this thread folds.** A 1px amber mark across the track at
 *  the backend's own fold point for *this* thread (`usage.fold` — the operator's
 *  threshold with the conversation's override already applied), drawn only while
 *  folding is armed. With it off there is nothing that will happen at that point, and a
 *  mark would promise a fold that isn't coming; the breakdown behind the click still
 *  shows where it would be. With no policy reported there is no tick at all.
 *
 *  **Click for the breakdown** (`ContextBreakdown`). The bar answers *how full*; the
 *  question it provokes is *full of what* — worth a click, because the answers lead to
 *  different actions. It opens in the View panel's own container (`RevealPopover`), so
 *  it arrives as a place made for it. */
export function ContextBar(props: ContextBarProps): JSX.Element {
  const fill = () => Math.min(100, Math.max(0, props.usage.fraction * 100));
  const fold = () => {
    const mark = foldMarker(props.usage);
    return mark?.active ? mark : null;
  };
  const figure = () => pct(props.usage.fraction * 100);

  return (
    <RevealPopover
      align="right"
      panelClass="w-88"
      trigger={({ open, setOpen }) => (
        <StatusCell
          active={open()}
          aria-expanded={open()}
          aria-label={`Context window ${figure()} full${
            fold() ? `, compacts at ${pct(fold()!.pct)}` : ""
          }`}
          onClick={() => setOpen(!open())}
          class="gap-2"
        >
          {/* Presentation only: the fill and the tick are positions along the backend's
              own fractions. */}
          <span aria-hidden="true" class="relative h-1 w-16 shrink-0 bg-line">
            <span
              class="absolute inset-y-0 left-0 bg-text"
              style={{ width: `${fill()}%` }}
            />
            <Show when={fold()}>
              {(mark) => (
                <span
                  class="absolute -top-0.5 h-2 w-px bg-warn"
                  style={{ left: `${mark().pct}%` }}
                />
              )}
            </Show>
          </span>
          <span class={FIGURE_TONE[props.usage.level]}>{figure()}</span>
        </StatusCell>
      )}
    >
      {() => (
        <ContextBreakdown usage={props.usage} lastRequest={props.lastRequest} />
      )}
    </RevealPopover>
  );
}
