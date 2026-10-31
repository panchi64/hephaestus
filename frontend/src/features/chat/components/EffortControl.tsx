import { createMemo, type JSX } from "solid-js";
import { effortOptions } from "~/lib/effort";
import { Select, Tooltip } from "~/ui";

/** How hard the model thinks on this thread's chat turns. Like the permission level it
 *  writes nothing on its own: the pick rides the next send. Disabled, not hidden, when
 *  the active endpoint's adapter reads no effort, so the control doesn't come and go
 *  with the model picker. */
export function EffortControl(props: {
  value: string;
  onChange: (level: string) => void;
  /** The level "Default" stands for: the endpoint's own. */
  fallback: string;
  enabled: boolean;
}): JSX.Element {
  const label = createMemo(() =>
    props.enabled
      ? "How hard the model thinks on this thread. Default uses the endpoint's setting."
      : "This endpoint doesn't use an effort setting.",
  );

  return (
    <Tooltip delay={600} side="top" label={label()}>
      <Select
        // A cell of the composer's status bar; the bar lowercases the label.
        cell
        value={props.value}
        disabled={!props.enabled}
        onChange={props.onChange}
        options={effortOptions(props.fallback)}
        aria-label="Effort"
      />
    </Tooltip>
  );
}
