import { Show, type JSX } from "solid-js";
import { Text } from "~/ui";
import type { WorkStep } from "../workShape";
import { Branch, type BranchEdge } from "./Branch";
import { ProcessRow, Sep } from "./ProcessRow";

/** The collapsed work log's latest step, hung off the header by an elbow.
 *
 *  The header says *why* the run is doing what it does (the model's narration);
 *  this row says *what* it last did. It is an ordinary `ProcessRow` with nothing
 *  to open, so it speaks the rows' own `glyph · Label · detail` anatomy and keeps
 *  their truncation rules by construction rather than by copy. Splitting reason
 *  from act is what lets each be read at a glance: a single line carrying both
 *  truncated the reason, which is the half worth reading.
 *
 *  It only exists while the log is shut. Open, the same step is the last row of
 *  the list, and a second copy of it directly above the first would be noise.
 *
 *  **While the run streams, it arrives by tracing in** (`.heph-trace-*`, §8): its
 *  branch draws itself down and across, and the row resolves as the stroke reaches
 *  it. The caller keys it on the step's block id, so each step the run folds in
 *  replays the arrival — the fold's proof of life — while a re-render of the same
 *  step does not. The socket blips the info tone as the trace reaches it and snaps
 *  back (`.heph-trace-pin`, stepped — the machine register). A settled run shows
 *  the row still: nothing is arriving.
 *
 *  It is the first branch on the log's trunk, and the last unless pinned rows
 *  hang beneath it, so the caller says which (`edge`). */
export function WorkLogLatest(props: {
  step: WorkStep;
  edge: BranchEdge;
  /** The run is streaming, so this step is arriving rather than being shown.
   *  Only then does it trace in: a keyed remount also happens on a thread opening
   *  and on the log folding shut again, and a trace-in and contact blip there
   *  would announce an arrival that never happened. Read once, at mount — the
   *  arrival is the mount. */
  live?: boolean;
}): JSX.Element {
  const live = props.live === true;
  return (
    <Branch edge={props.edge} trace={live}>
      <ProcessRow
        class={live ? "heph-trace-body" : undefined}
        open={false}
        foldable={false}
        onToggle={() => {}}
        icon={props.step.icon}
        iconClass={live ? "heph-trace-pin text-dim" : "text-dim"}
        label={props.step.label}
      >
        <Show when={props.step.detail}>
          {(detail) => (
            <>
              <Sep />
              <Text variant="micro" tone="dim" class="min-w-0 truncate">
                {detail()}
              </Text>
            </>
          )}
        </Show>
      </ProcessRow>
    </Branch>
  );
}
