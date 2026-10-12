import { createMemo, Show, type JSX } from "solid-js";
import { Combobox, Input, Select, Stack, Text, Textarea, Toggle } from "~/ui";
import {
  decodeModelValue,
  encodeModelValue,
  modelPickerGroups,
} from "~/lib/stores/models";
import {
  AGENT_NAME_PATTERN,
  PERMISSION_CEILINGS,
  WORKSPACE_OPTIONS,
  type AgentDraft,
} from "../model";

export type AgentFieldErrors = Partial<Record<string, string>>;

const NONE = "none";
const THREAD_MODEL = "";

/** The editable fields of an agent. Controlled by a draft the screen owns; a
 *  backend rejection renders verbatim on the field it named. */
export function AgentFields(props: {
  draft: AgentDraft;
  onChange: <K extends keyof AgentDraft>(key: K, value: AgentDraft[K]) => void;
  errors?: AgentFieldErrors;
  disabled?: boolean;
}): JSX.Element {
  const err = (field: string) => props.errors?.[field];
  const nameMalformed = () =>
    props.draft.name !== "" && !AGENT_NAME_PATTERN.test(props.draft.name);

  const modelValue = () =>
    props.draft.endpointId && props.draft.model
      ? encodeModelValue({
          endpointId: props.draft.endpointId,
          model: props.draft.model,
        })
      : THREAD_MODEL;

  const modelGroups = createMemo(() => {
    const groups = [
      {
        label: "Default",
        options: [{ value: THREAD_MODEL, label: "Thread's model" }],
      },
      ...modelPickerGroups(),
    ];
    const current = modelValue();
    const listed = groups.some((g) =>
      g.options.some((o) => o.value === current),
    );
    if (current && !listed) {
      groups.push({
        label: "Unavailable",
        options: [{ value: current, label: props.draft.model ?? current }],
      });
    }
    return groups;
  });

  function pickModel(value: string): void {
    const sel = value === THREAD_MODEL ? null : decodeModelValue(value);
    props.onChange("endpointId", sel?.endpointId ?? null);
    props.onChange("model", sel?.model ?? null);
  }

  return (
    <Stack gap={4}>
      <Input
        label="Name"
        value={props.draft.name}
        disabled={props.disabled}
        invalid={Boolean(err("name")) || nameMalformed()}
        hint={
          err("name") ??
          (nameMalformed()
            ? "Lowercase letters, digits, hyphens and underscores only"
            : undefined)
        }
        onInput={(e) => props.onChange("name", e.currentTarget.value)}
      />
      <Textarea
        label="Description"
        value={props.draft.description}
        disabled={props.disabled}
        invalid={Boolean(err("description"))}
        hint={err("description")}
        rows={3}
        onInput={(e) => props.onChange("description", e.currentTarget.value)}
      />
      <div class="flex flex-col gap-1">
        <Text variant="label" tone="dim">
          Model
        </Text>
        <Combobox
          groups={modelGroups()}
          value={modelValue()}
          onChange={pickModel}
          searchPlaceholder="Search models…"
          aria-label="Model"
        />
        <Show when={err("model") ?? err("endpoint_id")}>
          {(message) => (
            <Text variant="micro" tone="alert">
              {message()}
            </Text>
          )}
        </Show>
      </div>

      <Stack gap={2}>
        <Toggle
          label="Every tool"
          checked={props.draft.allTools}
          disabled={props.disabled}
          onChange={(v) => props.onChange("allTools", v)}
        />
        <Show when={!props.draft.allTools}>
          <Input
            label="Allowed tools"
            value={props.draft.toolsText}
            disabled={props.disabled}
            invalid={Boolean(err("tools"))}
            hint={err("tools") ?? "Comma-separated tool names"}
            placeholder="read_file, grep"
            onInput={(e) => props.onChange("toolsText", e.currentTarget.value)}
          />
        </Show>
      </Stack>
      <Input
        label="Denied tools"
        value={props.draft.denyText}
        disabled={props.disabled}
        invalid={Boolean(err("deny_tools"))}
        hint={err("deny_tools") ?? "Comma-separated; refused even if allowed"}
        onInput={(e) => props.onChange("denyText", e.currentTarget.value)}
      />

      <Select
        label="Permission ceiling"
        value={props.draft.permissionCeiling ?? NONE}
        disabled={props.disabled}
        invalid={Boolean(err("permission_ceiling"))}
        hint={err("permission_ceiling")}
        options={[
          { value: NONE, label: "None" },
          ...PERMISSION_CEILINGS.map((c) => ({ value: c, label: c })),
        ]}
        onChange={(v) =>
          props.onChange(
            "permissionCeiling",
            PERMISSION_CEILINGS.find((c) => c === v) ?? null,
          )
        }
      />
      <Select
        label="Workspace"
        value={props.draft.workspace}
        disabled={props.disabled}
        invalid={Boolean(err("workspace"))}
        hint={err("workspace")}
        options={WORKSPACE_OPTIONS.map((o) => ({ ...o }))}
        onChange={(v) => {
          const next = WORKSPACE_OPTIONS.find((o) => o.value === v);
          if (next) props.onChange("workspace", next.value);
        }}
      />

      <Input
        label="Request limit"
        type="number"
        min="1"
        value={props.draft.requestLimit}
        disabled={props.disabled}
        invalid={Boolean(err("request_limit"))}
        hint={err("request_limit") ?? "Blank for no limit"}
        onInput={(e) => props.onChange("requestLimit", e.currentTarget.value)}
      />
      <Input
        label="Wall-clock timeout (seconds)"
        type="number"
        min="1"
        value={props.draft.wallClockTimeoutS}
        disabled={props.disabled}
        invalid={Boolean(err("wall_clock_timeout_s"))}
        hint={err("wall_clock_timeout_s") ?? "Blank for no timeout"}
        onInput={(e) =>
          props.onChange("wallClockTimeoutS", e.currentTarget.value)
        }
      />

      <Toggle
        label="Can delegate"
        checked={props.draft.canDelegate}
        disabled={props.disabled}
        onChange={(v) => props.onChange("canDelegate", v)}
      />
      <Show when={props.draft.canDelegate}>
        <Input
          label="Max delegation depth"
          type="number"
          min="1"
          value={props.draft.maxDepth}
          disabled={props.disabled}
          invalid={Boolean(err("max_depth"))}
          hint={err("max_depth") ?? "Blank for the default"}
          onInput={(e) => props.onChange("maxDepth", e.currentTarget.value)}
        />
      </Show>
    </Stack>
  );
}
