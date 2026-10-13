import { For, Show, type JSX } from "solid-js";
import { Panel, Stack, StatusFlag, Text } from "~/ui";
import {
  currentManifest,
  hasChanges,
  publishChanges,
  sensitivityFlag,
  sensitivityLabel,
  widensAccess,
  type Plugin,
} from "../model";
import { PublishChanges } from "./PublishChanges";

function Empty(props: { children: string }): JSX.Element {
  return (
    <Text variant="meta" tone="dim">
      {props.children}
    </Text>
  );
}

/** The description, tools, reach and requirements of the version under review
 *  (the draft, else the active one) — and, above them, what a draft changes. */
export function PluginOverview(props: { plugin: Plugin }): JSX.Element {
  const manifest = () => currentManifest(props.plugin);
  const changes = () => {
    const draft = props.plugin.draft;
    return draft ? publishChanges(props.plugin.active, draft) : null;
  };
  return (
    <Stack gap={4}>
      <Show when={changes()}>
        {(c) => (
          <Show
            when={hasChanges(c())}
            fallback={
              <Panel label="Changes on publish">
                <Empty>The draft matches the active version.</Empty>
              </Panel>
            }
          >
            <Show when={widensAccess(c())}>
              <StatusFlag status="alert" class="self-start">
                WIDENS ACCESS
              </StatusFlag>
            </Show>
            <PublishChanges changes={c()} first={!props.plugin.active} />
          </Show>
        )}
      </Show>
      <Show when={manifest()}>
        {(m) => (
          <>
            <Panel label="Description">
              <Text variant="body">{m().description || "—"}</Text>
            </Panel>
            <Panel label={`Tools · ${m().tools.length}`}>
              <Show when={m().tools.length} fallback={<Empty>No tools.</Empty>}>
                <Stack gap={3}>
                  <For each={m().tools}>
                    {(t) => (
                      <Stack gap={1}>
                        <div class="flex flex-wrap items-center gap-2">
                          <Text variant="code" tone="bright">
                            {t.name}
                          </Text>
                          <StatusFlag status={sensitivityFlag(t.sensitivity)}>
                            {sensitivityLabel(t.sensitivity)}
                          </StatusFlag>
                        </div>
                        <Text variant="meta" tone="dim">
                          {t.description}
                        </Text>
                      </Stack>
                    )}
                  </For>
                </Stack>
              </Show>
            </Panel>
            <Show when={m().agents.length}>
              <Panel label={`Agents it adds · ${m().agents.length}`}>
                <Stack gap={1}>
                  <For each={m().agents}>
                    {(name) => (
                      <Text variant="code" tone="bright">
                        {name}
                      </Text>
                    )}
                  </For>
                </Stack>
              </Panel>
            </Show>
            <Show when={m().skills.length}>
              <Panel label={`Skills it adds · ${m().skills.length}`}>
                <Stack gap={1}>
                  <For each={m().skills}>
                    {(name) => (
                      <Text variant="code" tone="bright">
                        {name}
                      </Text>
                    )}
                  </For>
                </Stack>
              </Panel>
            </Show>
            <Show when={m().routines.length}>
              <Panel label={`Routines it adds · ${m().routines.length}`}>
                <Stack gap={1}>
                  <For each={m().routines}>
                    {(name) => (
                      <Text variant="code" tone="bright">
                        {name}
                      </Text>
                    )}
                  </For>
                </Stack>
              </Panel>
            </Show>
            <Panel label="Hosts it can reach">
              <Show
                when={m().egress.length}
                fallback={<Empty>No network access.</Empty>}
              >
                <Stack gap={1}>
                  <For each={m().egress}>
                    {(host) => (
                      <Text variant="code" tone="bright">
                        {host}
                      </Text>
                    )}
                  </For>
                </Stack>
              </Show>
            </Panel>
            <Panel label="Requirements">
              <Show
                when={m().requirements.length}
                fallback={<Empty>None.</Empty>}
              >
                <Stack gap={1}>
                  <For each={m().requirements}>
                    {(r) => (
                      <Text variant="code" tone="bright">
                        {r}
                      </Text>
                    )}
                  </For>
                </Stack>
              </Show>
            </Panel>
          </>
        )}
      </Show>
    </Stack>
  );
}
