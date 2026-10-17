import { For, Show, createResource, createSignal, type JSX } from "solid-js";
import {
  Button,
  Collapse,
  DiffView,
  Icon,
  Text,
  confirm,
  toast,
  type TextTone,
} from "~/ui";
import { isApiError } from "~/lib/api";
import {
  fetchChangeDiff,
  revertChanges,
  type ChangedFile,
  type ProjectChanges,
  type RevertReport,
} from "../data";

/**
 * What the thread changed in the operator's own project folders, and the way to put it
 * back. Unlike a worktree's branch there is nothing to merge: these edits are already in
 * their working copy, so the decision here is whether to keep them.
 */

const STATUS: Record<ChangedFile["status"], { word: string; tone: TextTone }> =
  {
    added: { word: "ADD", tone: "nominal" },
    modified: { word: "MOD", tone: "default" },
    deleted: { word: "DEL", tone: "alert" },
  };

function toastReport(report: RevertReport): void {
  const back = report.reverted.length;
  if (back) toast.success(`Put back ${back} file${back === 1 ? "" : "s"}.`);
  for (const skipped of report.skipped) {
    // The backend's reason, verbatim: it names why this one file was left alone.
    toast.error(`${skipped.path} was left as it is: ${skipped.reason}.`);
  }
}

function ChangeRow(props: {
  conversationId: string;
  file: ChangedFile;
  busy: boolean;
  onRevert: (ids: string[]) => void;
  fontStep?: number;
  softWrap?: boolean;
}): JSX.Element {
  const [open, setOpen] = createSignal(false);
  const [diff] = createResource(
    () => (open() ? props.file.id : undefined),
    (changeId) => fetchChangeDiff(props.conversationId, changeId),
  );
  const status = () => STATUS[props.file.status];
  const done = () => props.file.reverted;
  return (
    <div class="border-b border-line">
      <div class="flex min-w-0 items-center gap-2 px-3 py-2">
        <button
          type="button"
          onClick={() => setOpen(!open())}
          aria-expanded={open()}
          class="flex min-w-0 flex-1 items-baseline gap-2 text-left hover:text-bright"
        >
          <Text
            variant="meta"
            tone={done() ? "dim" : status().tone}
            class="w-10 shrink-0"
          >
            {done() ? "BACK" : status().word}
          </Text>
          <Text
            variant="body"
            tone={done() ? "dim" : "bright"}
            class="min-w-0 flex-1 truncate"
          >
            {props.file.path}
          </Text>
          <Icon
            name={open() ? "chevron-down" : "chevron-right"}
            size={12}
            class="shrink-0 text-dim"
          />
        </button>
        <Show
          when={props.file.revertible}
          fallback={
            <Text variant="meta" tone="dim" class="shrink-0">
              NO COPY
            </Text>
          }
        >
          <Show when={!done()}>
            <Button
              variant="ghost"
              disabled={props.busy}
              onClick={() => props.onRevert([props.file.id])}
              aria-label={`Put ${props.file.path} back as it was`}
            >
              Put back
            </Button>
          </Show>
        </Show>
      </div>
      <Collapse open={open()}>
        <div class="border-t border-line">
          <Show
            when={diff()?.patch}
            fallback={
              <Text variant="micro" tone="dim" class="block px-3 py-2">
                {diff.loading ? "Reading…" : "Not a text file."}
              </Text>
            }
          >
            {(patch) => (
              <DiffView
                diff={patch()}
                file={props.file.path}
                layout="hunks"
                fontStep={props.fontStep}
                softWrap={props.softWrap}
                class="max-h-80"
              />
            )}
          </Show>
        </div>
      </Collapse>
    </div>
  );
}

export function ProjectChangesList(props: {
  conversationId: string;
  projects: ProjectChanges[];
  onChanged: () => void;
  fontStep?: number;
  softWrap?: boolean;
}): JSX.Element {
  const [busy, setBusy] = createSignal(false);
  const pending = () =>
    props.projects.flatMap((p) =>
      p.files.filter((f) => !f.reverted && f.revertible),
    );

  const revert = async (ids?: string[]): Promise<void> => {
    setBusy(true);
    try {
      toastReport(await revertChanges(props.conversationId, ids));
      props.onChanged();
    } catch (err) {
      toast.error(
        isApiError(err) ? err.detail : "Unable to put the files back.",
      );
    } finally {
      setBusy(false);
    }
  };

  const revertAll = async (): Promise<void> => {
    const ok = await confirm({
      title: "Put every file back?",
      detail:
        "Each file this conversation changed in your projects goes back to how it was before. A file changed again since is left as it is.",
      confirmLabel: "Put back",
      tone: "alert",
    });
    if (ok) await revert();
  };

  return (
    <div class="flex min-h-0 flex-col">
      <div class="flex shrink-0 flex-wrap items-center gap-2 px-3 py-2">
        <Text variant="micro" tone="dim" class="min-w-0 flex-1">
          Changed in your own project folders. These edits are already in your
          working copy.
        </Text>
        <Button
          variant="danger"
          disabled={busy() || pending().length === 0}
          onClick={() => void revertAll()}
        >
          Put all back
        </Button>
      </div>
      <For each={props.projects}>
        {(project) => (
          <>
            <div class="flex items-baseline gap-2 border-y border-line bg-raised px-3 py-1">
              <Text variant="plate">{project.name}</Text>
              <Text variant="micro" tone="dim" class="min-w-0 truncate">
                {project.homePath}
              </Text>
            </div>
            <For each={project.files}>
              {(file) => (
                <ChangeRow
                  conversationId={props.conversationId}
                  file={file}
                  busy={busy()}
                  onRevert={(ids) => void revert(ids)}
                  fontStep={props.fontStep}
                  softWrap={props.softWrap}
                />
              )}
            </For>
          </>
        )}
      </For>
    </div>
  );
}
