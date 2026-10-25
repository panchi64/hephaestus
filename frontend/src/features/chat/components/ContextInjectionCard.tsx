import { Show, type JSX } from "solid-js";
import { Collapse, Text, copyToClipboard, Icon, REVEAL_BASE } from "~/ui";
import { compactCount } from "~/lib/format";
import { INJECTION_ICON, segmentLabel } from "../contextLabels";
import type { ContextInjection } from "../model";
import { ProcessRow, Sep, createAdoptedOpen } from "./ProcessRow";

/** Where in the request a block landed, in the operator's words.
 *
 *  Worth a word on the row because the placements cost differently and the operator can
 *  act on the difference: a block at the head is re-sent ahead of the whole history every
 *  turn, so churn there invalidates the inference engine's prompt-prefix cache from byte
 *  0; one at the tail leaves that prefix byte-stable; one on a compaction summary is
 *  stored with the checkpoint and replayed until the next fold. */
const PLACEMENT: Record<ContextInjection["placement"], string> = {
  instructions: "at the prompt head",
  prompt: "at the turn's tail",
  summary: "on the compaction summary",
};

/** One block of context the chassis put in front of the model — a project's instruction
 *  files, the skill catalog, the plan reminder, the date.
 *
 *  **It shares the log's anatomy and dashes its trace.** Every other row in a turn is
 *  something the model *did*. This is the opposite kind of fact: nobody in the
 *  conversation wrote it and the model did not ask for it — we put it there. So it keeps
 *  `ProcessRow` (the column only reads as one sequence if its rows share an anatomy, §7)
 *  but hangs off the trunk by a dashed elbow into a dashed diamond (`Branch chassis`),
 *  and takes no hover fill. Glyph, tone and the dashed trace all say the same thing at a
 *  glance, which is what the separation has to survive on: an operator scanning a turn
 *  should never have to read a row to know whether the model or the chassis is speaking.
 *
 *  The trailing figure is the block's **token cost**, in the slot a tool call spends on
 *  elapsed time — the honest analogue, since what an injection costs is window and what a
 *  call costs is seconds. `~` because it is the same coarse estimate the context gauge
 *  renders, deliberately measured the same way so the two agree.
 *
 *  `open` makes expand/collapse controlled (expand-all/collapse-all); undefined leaves the
 *  card closed, which is where an injection belongs at rest — it is the frame around the
 *  work, not the work. */
export function ContextInjectionCard(props: {
  injection: ContextInjection;
  open?: boolean;
}): JSX.Element {
  const { open, toggle } = createAdoptedOpen(props);
  const copyText = (e: MouseEvent): void => {
    e.stopPropagation();
    copyToClipboard(props.injection.text, "Injected context");
  };
  return (
    <div class="group/context">
      {/* No `hover:bg-raised`: that fill is for the model's calls, the work an operator
          opens to inspect, and this one deliberately has none — the same posture the
          settled reasoning row takes. */}
      <ProcessRow
        open={open()}
        onToggle={toggle}
        icon={INJECTION_ICON}
        frame="diamond"
        dashed
        label={segmentLabel(props.injection.contributor)}
        title={`Context injected by ${props.injection.contributor}`}
        trailing={
          <>
            <Text variant="micro" tone="dim" class="tabular-nums">
              ~{compactCount(props.injection.tokens, true)}
            </Text>
            <button
              type="button"
              aria-label="Copy injected context"
              onClick={copyText}
              class={`${REVEAL_BASE} text-dim hover:text-bright group-hover/context:opacity-100`}
            >
              <Icon name="copy" size={12} />
            </button>
          </>
        }
      >
        {/* The one word that says this is not work: "Injected", then where it landed.
            Colour is never the sole carrier of the distinction (§12) — the glyph and the
            missing card are visual, this is the same fact in words. */}
        <Sep />
        <Text variant="micro" tone="dim" class="min-w-0 shrink-0">
          Injected
        </Text>
        <Sep />
        <Text variant="micro" tone="dim" class="min-w-0 truncate">
          {PLACEMENT[props.injection.placement]}
        </Text>
      </ProcessRow>
      <Collapse open={open()}>
        <div class="flex flex-col gap-1 px-2 py-1.5">
          <Text
            variant="micro"
            tone="dim"
            class="max-h-64 overflow-y-auto whitespace-pre-wrap break-words"
          >
            {props.injection.text}
          </Text>
          {/* A capped block ends mid-file, and an operator who cannot tell that from a
              file that simply ends there would read the wrong thing into the gap. */}
          <Show when={props.injection.truncated}>
            <Text variant="micro" tone="dim" class="italic">
              Shown to here; the model was given the whole block.
            </Text>
          </Show>
        </div>
      </Collapse>
    </div>
  );
}
