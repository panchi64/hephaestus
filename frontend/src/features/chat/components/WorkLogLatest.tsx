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
 *  **A step that arrives straight into the fold traces in** (`Branch trace`, §8).
 *  The caller decides that; a step moving here from the pinned run already
 *  arrived there, and does not replay.
 *
 *  It is the first branch on the log's trunk, and the last unless pinned rows
 *  hang beneath it, so the caller says which (`edge`). */
export function WorkLogLatest(props: {
  step: WorkStep;
  edge: BranchEdge;
  trace?: boolean;
}): JSX.Element {
  return (
    <Branch edge={props.edge} trace={props.trace}>
      <ProcessRow
        open={false}
        foldable={false}
        onToggle={() => {}}
        icon={props.step.icon}
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
