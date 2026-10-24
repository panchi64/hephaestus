import { For, Show, type JSX } from "solid-js";
import { ConsoleGroup, Stack, Text } from "~/ui";
import type { RewindPreview } from "../data/rewind";

import { rewindRows } from "../rewindRestore";

/** The dry run of a file rewind, as the body of the confirmation dialog: the files in a
 *  ruled group, then what the restore does and does not cover. */
export function RewindRestoreBody(props: {
  preview: RewindPreview;
  /** The files changed since the last preview, so this one replaces it. */
  changed: boolean;
}): JSX.Element {
  const rows = () => rewindRows(props.preview);
  return (
    <Stack gap={3}>
      <Show when={props.changed}>
        <Text variant="micro" tone="warn">
          Files changed since the last check. This is the current list.
        </Text>
      </Show>
      <ConsoleGroup label="Files" right={<Count n={rows().length} />} flush>
        <ul class="max-h-64 divide-y divide-line overflow-y-auto">
          <For each={rows()}>
            {(row) => (
              <li class="flex flex-col gap-0.5 px-2 py-1">
                <div class="flex items-baseline gap-2">
                  <Text
                    variant="code"
                    tone="bright"
                    class="min-w-0 flex-1 truncate"
                    title={`${row.projectId}/${row.path}`}
                  >
                    {row.path}
                  </Text>
                  <Text variant="meta" tone={row.mark.tone}>
                    {row.mark.label}
                  </Text>
                </div>
                <Show when={row.mark.note}>
                  <Text variant="micro" tone="dim" class="break-words">
                    {row.mark.note}
                  </Text>
                </Show>
              </li>
            )}
          </For>
        </ul>
      </ConsoleGroup>
      <Text variant="micro" tone="dim">
        Covered: file-tool writes, VM shell changes brought back to a project,
        and sub-agent edits. Not covered: host-reach shell commands, files over
        4 MiB, and VM scratch changes made only by the shell.
      </Text>
    </Stack>
  );
}

function Count(props: { n: number }): JSX.Element {
  return (
    <Text variant="meta" tone="dim" class="tabular-nums">
      {props.n}
    </Text>
  );
}
