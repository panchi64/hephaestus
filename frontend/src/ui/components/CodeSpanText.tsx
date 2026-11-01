import { For, type JSX } from "solid-js";
import { Text, type TextTone, type TextVariant } from "../primitives/Text";
import { codeSpans } from "./codeSpans";

export interface CodeSpanTextProps {
  /** Plain text whose `backtick` spans render as inline code. */
  children: string;
  variant?: TextVariant;
  tone?: TextTone;
  class?: string;
}

/** A sentence that names identifiers: the prose stays in its variant, each code span
 *  drops to mono. */
export function CodeSpanText(props: CodeSpanTextProps): JSX.Element {
  return (
    <Text
      variant={props.variant ?? "caption"}
      tone={props.tone}
      class={props.class}
    >
      <For each={codeSpans(props.children)}>
        {(span) =>
          span.code ? (
            <code class="font-mono text-text">{span.text}</code>
          ) : (
            span.text
          )
        }
      </For>
    </Text>
  );
}
