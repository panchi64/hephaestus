import { For, Show, type JSX } from "solid-js";
import { Panel, Stack, StatusFlag, Text } from "~/ui";
import {
  sensitivityFlag,
  sensitivityLabel,
  type PublishChanges as Changes,
} from "../model";

function Line(props: {
  tag: string;
  tone: "nominal" | "alert" | "warn" | "idle";
  children: JSX.Element;
}): JSX.Element {
  return (
    <div class="flex flex-wrap items-baseline gap-2">
      <StatusFlag status={props.tone}>{props.tag}</StatusFlag>
      <Text variant="code" tone="bright" class="min-w-0 break-all">
        {props.children}
      </Text>
    </div>
  );
}

/** What the operator approves by publishing: every host, secret, tool, agent and skill that is
 *  new, gone or more sensitive than in the active version. */
export function PublishChanges(props: {
  changes: Changes;
  /** True when there is no active version, so all of it is new. */
  first: boolean;
}): JSX.Element {
  const c = () => props.changes;
  return (
    <Panel
      label={props.first ? "Approved on first publish" : "Changes on publish"}
    >
      <Stack gap={2}>
        <For each={c().egress.added}>
          {(host) => (
            <Line tag="NEW HOST" tone="alert">
              {host}
            </Line>
          )}
        </For>
        <For each={c().secrets.added}>
          {(s) => (
            <Line tag="NEW SECRET" tone="alert">
              {s.name} → {s.host}
            </Line>
          )}
        </For>
        <For each={c().secrets.rehosted}>
          {(s) => (
            <Line tag="SECRET HOST" tone="alert">
              {s.name}: {s.from} → {s.to}
            </Line>
          )}
        </For>
        <For each={c().tools.added}>
          {(t) => (
            <div class="flex flex-wrap items-baseline gap-2">
              <StatusFlag status="nominal">NEW TOOL</StatusFlag>
              <Text variant="code" tone="bright">
                {t.name}
              </Text>
              <StatusFlag status={sensitivityFlag(t.sensitivity)}>
                {sensitivityLabel(t.sensitivity)}
              </StatusFlag>
            </div>
          )}
        </For>
        <For each={c().tools.sensitivity}>
          {(s) => (
            <Line tag="SENSITIVITY" tone={s.escalated ? "alert" : "warn"}>
              {s.name}: {sensitivityLabel(s.from)} → {sensitivityLabel(s.to)}
            </Line>
          )}
        </For>
        <For each={c().agents.added}>
          {(name) => (
            <Line tag="NEW AGENT" tone="alert">
              {name}
            </Line>
          )}
        </For>
        <For each={c().skills.added}>
          {(name) => (
            <Line tag="NEW SKILL" tone="alert">
              {name}
            </Line>
          )}
        </For>
        <For each={c().routines.added}>
          {(name) => (
            <Line tag="NEW ROUTINE" tone="alert">
              {name}
            </Line>
          )}
        </For>
        <For each={c().requirements.added}>
          {(r) => (
            <Line tag="NEW REQUIREMENT" tone="warn">
              {r}
            </Line>
          )}
        </For>
        <For each={c().egress.removed}>
          {(host) => (
            <Line tag="HOST REMOVED" tone="idle">
              {host}
            </Line>
          )}
        </For>
        <For each={c().secrets.removed}>
          {(name) => (
            <Line tag="SECRET REMOVED" tone="idle">
              {name}
            </Line>
          )}
        </For>
        <For each={c().tools.removed}>
          {(name) => (
            <Line tag="TOOL REMOVED" tone="idle">
              {name}
            </Line>
          )}
        </For>
        <For each={c().agents.removed}>
          {(name) => (
            <Line tag="AGENT REMOVED" tone="idle">
              {name}
            </Line>
          )}
        </For>
        <For each={c().skills.removed}>
          {(name) => (
            <Line tag="SKILL REMOVED" tone="idle">
              {name}
            </Line>
          )}
        </For>
        <For each={c().routines.removed}>
          {(name) => (
            <Line tag="ROUTINE REMOVED" tone="idle">
              {name}
            </Line>
          )}
        </For>
        <For each={c().requirements.removed}>
          {(r) => (
            <Line tag="REQUIREMENT REMOVED" tone="idle">
              {r}
            </Line>
          )}
        </For>
        <Show when={c().descriptionChanged}>
          <Text variant="meta" tone="dim">
            The description changed.
          </Text>
        </Show>
        <Text variant="meta" tone="dim">
          The History tab shows the file-by-file diff.
        </Text>
      </Stack>
    </Panel>
  );
}
