import type { JSX } from "solid-js";
import { Row, Stack, Text } from "~/ui";

/** One setting: its name and a line about it on the left, its control on the right. */
export function SettingRow(props: {
  label: string;
  description: JSX.Element;
  class?: string;
  children: JSX.Element;
}): JSX.Element {
  return (
    <Row align="center" justify="between" class={props.class}>
      <Stack gap={1}>
        <Text variant="label" tone="default">
          {props.label}
        </Text>
        <Text variant="caption" tone="dim">
          {props.description}
        </Text>
      </Stack>
      {props.children}
    </Row>
  );
}
