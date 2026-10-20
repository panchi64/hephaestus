import { createSignal, For, type JSX } from "solid-js";
import { Button, confirm, Panel, Stack, toast } from "~/ui";
import { clearData } from "../data";
import { clearedMessage, type DataKind } from "../clearData";
import { SettingRow } from "./SettingRow";

interface ClearRow {
  kind: DataKind;
  label: string;
  description: string;
  confirmTitle: string;
  confirmDetail: string;
}

const ROWS: ClearRow[] = [
  {
    kind: "chats",
    label: "Chats",
    description: "Every conversation, with its history, recap and images.",
    confirmTitle: "Delete every chat?",
    confirmDetail:
      "Every conversation goes, including pinned and project threads, with their images and any unmerged code branches. Chats with a turn still running are left in place.",
  },
  {
    kind: "memories",
    label: "Memories",
    description: "Everything the agent has remembered about you.",
    confirmTitle: "Delete every memory?",
    confirmDetail: "The agent forgets everything it has saved, pinned or not.",
  },
  {
    kind: "notifications",
    label: "Notifications",
    description: "Read and unread.",
    confirmTitle: "Delete every notification?",
    confirmDetail: "Read and unread notifications are both deleted.",
  },
  {
    kind: "uploads",
    label: "Uploads",
    description: "Every file you have uploaded, including chat attachments.",
    confirmTitle: "Delete every upload?",
    confirmDetail:
      "Every uploaded file is deleted and removed from the chats that attached it.",
  },
];

export function ClearDataSection(): JSX.Element {
  const [busy, setBusy] = createSignal<DataKind | null>(null);

  const run = async (row: ClearRow): Promise<void> => {
    const ok = await confirm({
      title: row.confirmTitle,
      detail: `${row.confirmDetail} This cannot be undone.`,
      confirmLabel: `Delete ${row.label.toLowerCase()}`,
      tone: "alert",
      requireText: "delete",
    });
    if (!ok) return;
    setBusy(row.kind);
    try {
      toast.success(clearedMessage(row.kind, await clearData(row.kind)));
    } catch {
      toast.error(`Couldn't delete ${row.label.toLowerCase()}.`);
    } finally {
      setBusy(null);
    }
  };

  return (
    <Panel label="Clear data">
      <Stack gap={3}>
        <For each={ROWS}>
          {(row) => (
            <SettingRow label={row.label} description={row.description}>
              <Button
                variant="danger"
                size="sm"
                disabled={busy() !== null}
                onClick={() => void run(row)}
              >
                {busy() === row.kind ? "Deleting…" : "Delete all"}
              </Button>
            </SettingRow>
          )}
        </For>
      </Stack>
    </Panel>
  );
}
