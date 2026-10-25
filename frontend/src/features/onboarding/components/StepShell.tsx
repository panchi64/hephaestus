import { Show, type JSX } from "solid-js";
import {
  ActionButtons,
  ActionFrame,
  Reveal,
  Stack,
  Text,
  type ActionButtonSpec,
} from "~/ui";

/** One step, top to bottom: the machine's status block, Talos's one line, and the single
 *  live question frame. `buttons` gives the frame a footer; omit it when the body brings
 *  its own (a `ChoiceList`). */
export function StepShell(props: {
  status?: JSX.Element;
  talos: string;
  title: string;
  aside?: JSX.Element;
  buttons?: ActionButtonSpec[];
  hint?: string;
  children: JSX.Element;
}): JSX.Element {
  return (
    <Reveal motion="rise" distance={10}>
      <Stack gap={4}>
        {props.status}
        <Text variant="body">{props.talos}</Text>
        <ActionFrame tone="question" title={props.title} aside={props.aside}>
          {props.children}
          <Show when={props.buttons}>
            {(buttons) => (
              <div class="heph-choice-foot border-t border-line">
                <Text variant="micro" tone="dim">
                  {props.hint ?? ""}
                </Text>
                <ActionButtons buttons={buttons()} />
              </div>
            )}
          </Show>
        </ActionFrame>
      </Stack>
    </Reveal>
  );
}
