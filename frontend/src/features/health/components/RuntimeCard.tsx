import { For, type JSX } from "solid-js";
import { Field, Panel, Text } from "~/ui";
import type { RoleModel, RuntimeInfo } from "../model";

function roleValue(r: RoleModel): string {
  if (!r.endpoint && !r.model) return "Unbound";
  const bound = [r.endpoint, r.model].filter(Boolean).join(" · ");
  return r.implicit ? `${bound} (default)` : bound;
}

/** What this backend runs on and what it runs. Grayscale: none of it is a fault. */
export function RuntimeCard(props: { runtime: RuntimeInfo }): JSX.Element {
  return (
    <Panel label="Runtime" class="@container">
      <div class="grid grid-cols-2 gap-x-6 gap-y-4 @min-[40rem]:grid-cols-4">
        <Field label="Odysseus" value={props.runtime.version} />
        <Field label="Pydantic AI" value={props.runtime.pydanticAi} />
        <Field label="Harness" value={props.runtime.pydanticAiHarness} />
        <Field label="Python" value={props.runtime.python} />
      </div>
      <div class="mt-4 grid grid-cols-[auto_1fr] items-baseline gap-x-6 gap-y-1 border-t border-line pt-4">
        <For each={props.runtime.roles}>
          {(r) => (
            <>
              <Text variant="meta" tone="dim" class="uppercase">
                {r.role}
              </Text>
              <Text
                variant="micro"
                tone={r.endpoint || r.model ? "bright" : "dim"}
                class="truncate"
              >
                {roleValue(r)}
              </Text>
            </>
          )}
        </For>
        <Text variant="meta" tone="dim" class="uppercase">
          Tools
        </Text>
        <Text variant="micro" tone="bright">
          {`${props.runtime.tools.enabled} of ${props.runtime.tools.total} enabled`}
        </Text>
        <Text variant="meta" tone="dim" class="uppercase">
          Tracing
        </Text>
        <Text variant="micro" tone={props.runtime.tracing ? "bright" : "dim"}>
          {props.runtime.tracing ? "On, kept for 30 days" : "Off"}
        </Text>
      </div>
    </Panel>
  );
}
