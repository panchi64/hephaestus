import { splitProps } from "solid-js";
import type { JSX } from "solid-js";
import { Text } from "../primitives/Text";
import { Ellipsis } from "./Ellipsis";

export interface LoadingTextProps {
  /** Defaults to "Loading". No trailing ellipsis: the stepped dots supply it. */
  label?: string;
  class?: string;
}

/** The system's only loading affordance: a dim label with a stepped ellipsis,
 *  never a spinner (§6 states, §8 motion). */
export function LoadingText(props: LoadingTextProps): JSX.Element {
  const [local] = splitProps(props, ["label", "class"]);
  return (
    <Text variant="label" tone="dim" class={local.class}>
      <Ellipsis>{local.label ?? "Loading"}</Ellipsis>
    </Text>
  );
}
