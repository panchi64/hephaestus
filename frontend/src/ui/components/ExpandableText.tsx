import { Show, createSignal, splitProps, type JSX } from "solid-js";
import { cx } from "../cx";
import { clipText } from "./clipText";
import { Text, type TextTone, type TextVariant } from "../primitives/Text";

export interface ExpandableTextProps {
  /** Full text; collapsed to `limit` chars with an inline MORE/LESS toggle. */
  text: string;
  /** Max characters before truncation. Default 160. */
  limit?: number;
  variant?: TextVariant;
  tone?: TextTone;
  class?: string;
}

/**
 * Inline truncation with a MORE/LESS toggle — the reusable answer to "detail
 * is cut off and unreachable" (run output, long memories, tool descriptions).
 * For very large content prefer a Drawer/Modal; this is for a sentence or two.
 */
export function ExpandableText(props: ExpandableTextProps): JSX.Element {
  const [local] = splitProps(props, [
    "text",
    "limit",
    "variant",
    "tone",
    "class",
  ]);
  const [open, setOpen] = createSignal(false);
  const limit = () => local.limit ?? 160;
  const truncatable = () => local.text.length > limit();
  const shown = () => (open() ? local.text : clipText(local.text, limit()));

  return (
    <Text
      variant={local.variant ?? "body"}
      tone={local.tone}
      class={cx("whitespace-pre-wrap break-words", local.class)}
    >
      {shown()}
      <Show when={truncatable()}>
        {" "}
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          class="text-label font-sans font-medium text-dim transition-colors hover:text-bright"
        >
          {open() ? "Less" : "More"}
        </button>
      </Show>
    </Text>
  );
}
