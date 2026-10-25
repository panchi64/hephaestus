import { createSignal, For, type JSX } from "solid-js";
import { Button, Chip, Input, Row, Stack, Text } from "~/ui";

const SNOWFLAKE = /^\d+$/;

/** An editable list of Discord ids; each change is handed to `onChange` whole. */
export function IdListField(props: {
  label: string;
  hint: string;
  ids: string[];
  onChange: (ids: string[]) => void;
}): JSX.Element {
  const [draft, setDraft] = createSignal("");
  const value = () => draft().trim();
  const valid = () => SNOWFLAKE.test(value()) && !props.ids.includes(value());

  const add = () => {
    if (!valid()) return;
    props.onChange([...props.ids, value()]);
    setDraft("");
  };

  return (
    <Stack gap={2}>
      <Stack gap={1}>
        <Text variant="label" tone="default">
          {props.label}
        </Text>
        <Text variant="micro" tone="dim">
          {props.hint}
        </Text>
      </Stack>
      <Row gap={2} wrap>
        <For
          each={props.ids}
          fallback={
            <Text variant="micro" tone="dim">
              None yet.
            </Text>
          }
        >
          {(id) => (
            <Chip
              leading="close"
              onClick={() => props.onChange(props.ids.filter((x) => x !== id))}
            >
              {id}
            </Chip>
          )}
        </For>
      </Row>
      <Row gap={2} align="center">
        <Input
          aria-label={`Add to ${props.label}`}
          placeholder="Numeric id"
          inputmode="numeric"
          value={draft()}
          invalid={value() !== "" && !valid()}
          onInput={(e) => setDraft(e.currentTarget.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") add();
          }}
        />
        <Button size="sm" leading="plus" disabled={!valid()} onClick={add}>
          ADD
        </Button>
      </Row>
    </Stack>
  );
}
