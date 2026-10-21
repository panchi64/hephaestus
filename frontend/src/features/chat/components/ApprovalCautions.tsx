import { For, Show, type JSX } from "solid-js";
import { Row, Stack, Text } from "~/ui";
import { cautionLines, offersGrant } from "../approvalCautions";
import type { GrantWidth } from "../commandScope";
import type { ApprovalCautions as Cautions } from "../model";
import { ConversationGrantToggle } from "./ConversationGrantToggle";

/**
 * What makes this approval different from an ordinary one, on the card that asks.
 *
 * The label is the mono `meta` word in the warn accent and the reason follows in dim
 * `micro` sentence form, the pairing `CommandBoundary` uses for an unfenced command: a
 * bare word leaves the operator with nothing to act on, and the accent stays on the one
 * word that names the caution. Renders nothing when neither is set.
 */
export function ApprovalCautions(props: { cautions: Cautions }): JSX.Element {
  const lines = () => cautionLines(props.cautions);
  return (
    <Stack gap={1}>
      <For each={lines()}>
        {(line) => (
          <Row gap={2} align="start">
            <Text variant="meta" tone="warn" class="shrink-0">
              {line.label}
            </Text>
            <Text variant="micro" tone="dim" class="min-w-0 break-words">
              {line.reason}
            </Text>
          </Row>
        )}
      </For>
    </Stack>
  );
}

/** The cautions, then the standing-grant opt-in unless the call is once-only. Sits above
 *  the verdict so the opt-in is set before the click that captures it. */
export function CautionsAndGrant(props: {
  cautions: Cautions;
  /** False where a standing grant is not coherent at all, as for a submitted plan. */
  grantable?: boolean;
  command?: string;
  toolName: string;
  width?: GrantWidth;
  disabled?: boolean;
  onChange: (width: GrantWidth) => void;
}): JSX.Element {
  return (
    <>
      <ApprovalCautions cautions={props.cautions} />
      <Show when={props.grantable !== false && offersGrant(props.cautions)}>
        <ConversationGrantToggle
          command={props.command}
          toolName={props.toolName}
          width={props.width}
          disabled={props.disabled}
          onChange={props.onChange}
        />
      </Show>
    </>
  );
}
