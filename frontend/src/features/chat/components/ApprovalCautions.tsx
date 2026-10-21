import { For, type JSX } from "solid-js";
import { Row, Stack, Text } from "~/ui";
import { cautionLines } from "../approvalCautions";
import type { ApprovalCautions as Cautions } from "../model";

/**
 * What makes this approval different from an ordinary one, on the card that asks.
 *
 * Both lines are caution, so both are `warn` and neither is a new accent. The label is
 * the mono `meta` word and the reason follows in sentence form, the pairing
 * `CommandBoundary` uses for an unfenced command: a bare word leaves the operator with
 * nothing to act on. Renders nothing when neither is set.
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
            <Text variant="micro" tone="warn" class="min-w-0 break-words">
              {line.reason}
            </Text>
          </Row>
        )}
      </For>
    </Stack>
  );
}
