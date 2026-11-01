import { createEffect, Show, type JSX } from "solid-js";
import {
  ActionButtons,
  ActionFrame,
  Reveal,
  Stack,
  type ActionButtonSpec,
} from "~/ui";
import { useBriefingSlot } from "./briefingSlot";

/** One step under Talos's briefing: the machine's status block, then the single live
 *  question frame. The briefing goes to the screen's one `TalosBriefing` rather than
 *  rendering here. `buttons` gives the frame a footer; omit it when the body brings its
 *  own (a `ChoiceList`). */
export function StepShell(props: {
  briefing: string;
  status?: JSX.Element;
  title: string;
  aside?: JSX.Element;
  buttons?: ActionButtonSpec[];
  children: JSX.Element;
}): JSX.Element {
  const setBriefing = useBriefingSlot();
  createEffect(() => setBriefing(props.briefing));

  return (
    <Reveal motion="rise" distance={10}>
      <Stack gap={4}>
        {props.status}
        <ActionFrame tone="question" title={props.title} aside={props.aside}>
          {props.children}
          <Show when={props.buttons}>
            {(buttons) => (
              <div class="heph-choice-foot border-t border-line">
                <div class="ml-auto">
                  <ActionButtons buttons={buttons()} />
                </div>
              </div>
            )}
          </Show>
        </ActionFrame>
      </Stack>
    </Reveal>
  );
}
