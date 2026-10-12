import { createSignal, For, onMount, Show, type JSX } from "solid-js";
import { createStore, produce, reconcile } from "solid-js/store";
import { isApiError } from "~/lib/api";
import {
  Button,
  Icon,
  Menu,
  Panel,
  Row,
  Stack,
  Tabs,
  Text,
  Toggle,
  Tooltip,
  toast,
  type IconName,
  type MenuItem,
} from "~/ui";
import {
  LibraryFileBrowser,
  type LibraryFileSource,
  type ViewMode,
} from "./LibraryFileBrowser";

export interface LibraryProperty {
  label: string;
  value: JSX.Element;
}

/** Why a save was refused, shaped for the line under the path header. */
function saveMessage(err: unknown): string {
  if (!isApiError(err)) return "Could not save.";
  return err.field ? `${err.field}: ${err.detail}` : err.detail;
}

/** The read view both Skills and Agents open to: back link, a header with the
 *  item's name, state and actions, and Overview / Contents tabs. Edit does not
 *  navigate — it turns the Contents pane into an editor in place, and the
 *  header's Export / publish / Edit are replaced by Cancel and Save. */
export function LibraryDetail(props: {
  backLabel: string;
  onBack: () => void;
  icon: IconName;
  name: string;
  flags: JSX.Element;
  meta: string;
  onExport: () => void;
  published: boolean;
  /** Absent where the item cannot be published (read-only). */
  onTogglePublished?: () => void;
  /** Present for items that can be pinned; the toggle only applies while published. */
  pinned?: boolean;
  onTogglePinned?: () => void;
  pinHint?: string;
  canEdit: boolean;
  startEditing?: boolean;
  menuItems: MenuItem[];
  description: string;
  /** The Overview's key/value list; may offer inputs while editing. */
  properties: (editing: boolean) => LibraryProperty[];
  /** The first file is the item's own markdown file. */
  files: LibraryFileSource[];
  /** Persist one file's full text. Throws the backend's rejection. */
  saveFile: (path: string, text: string) => Promise<void>;
  onDeleteFile?: (path: string) => Promise<void>;
  onAddFile?: (relpath: string) => Promise<void>;
  /** Unsaved edits made outside the files (the agent's model). */
  extraDirty?: boolean;
  saveExtra?: () => Promise<void>;
  resetExtra?: () => void;
}): JSX.Element {
  const [tab, setTab] = createSignal<"overview" | "contents">("overview");
  const [editing, setEditing] = createSignal(false);
  const [mode, setMode] = createSignal<ViewMode>("preview");
  const [drafts, setDrafts] = createStore<Record<string, string>>({});
  const [saved, setSaved] = createStore<Record<string, string>>({});
  const [version, setVersion] = createSignal(0);
  const [error, setError] = createSignal<string | null>(null);
  const [busy, setBusy] = createSignal(false);

  const dirtyPaths = () =>
    Object.keys(drafts).filter((p) => drafts[p] !== saved[p]);
  const dirty = () => dirtyPaths().length > 0 || Boolean(props.extraDirty);

  function startEdit(): void {
    setEditing(true);
    setMode("source");
    setError(null);
  }
  onMount(() => {
    if (!props.startEditing || !props.canEdit) return;
    startEdit();
    setTab("contents");
  });

  function cancel(): void {
    setDrafts(reconcile({}));
    props.resetExtra?.();
    setError(null);
    setEditing(false);
    setMode("preview");
  }

  async function save(): Promise<void> {
    if (!dirty() || busy()) return;
    setBusy(true);
    setError(null);
    const main = props.files[0]?.path;
    // The item's own file first: a bad name or frontmatter stops the save
    // before any bundle file is touched.
    const paths = [
      ...dirtyPaths().filter((p) => p === main),
      ...dirtyPaths().filter((p) => p !== main),
    ];
    try {
      for (const path of paths) await props.saveFile(path, drafts[path]);
      await props.saveExtra?.();
    } catch (err) {
      setError(saveMessage(err));
      setBusy(false);
      return;
    }
    setDrafts(reconcile({}));
    setVersion((n) => n + 1);
    setEditing(false);
    setMode("preview");
    setBusy(false);
    toast.success("Saved");
  }

  async function addFile(relpath: string): Promise<void> {
    try {
      await props.onAddFile?.(relpath);
      setVersion((n) => n + 1);
    } catch (err) {
      setError(saveMessage(err));
    }
  }

  async function deleteFile(path: string): Promise<void> {
    try {
      await props.onDeleteFile?.(path);
      setDrafts(
        produce((d) => {
          delete d[path];
        }),
      );
      setVersion((n) => n + 1);
    } catch (err) {
      setError(saveMessage(err));
    }
  }

  return (
    <Stack gap={4}>
      <Button
        variant="ghost"
        size="sm"
        leading="chevron-left"
        onClick={props.onBack}
        class="self-start"
      >
        {props.backLabel}
      </Button>

      <header class="flex flex-wrap items-start justify-between gap-3">
        <Row gap={3} align="center">
          <span class="flex size-10 shrink-0 items-center justify-center border border-line bg-surface text-dim">
            <Icon name={props.icon} size={20} />
          </span>
          <Stack gap={1}>
            <Row gap={2} align="center" wrap>
              <Text variant="readout" tone="bright">
                {props.name}
              </Text>
              {props.flags}
              <Show when={editing()}>
                <Text variant="micro" tone="dim">
                  EDITING
                </Text>
              </Show>
            </Row>
            <Text variant="micro" tone="dim">
              {props.meta}
            </Text>
          </Stack>
        </Row>

        <Row gap={2} align="center">
          <Show
            when={editing()}
            fallback={
              <>
                <Button
                  variant="default"
                  size="sm"
                  leading="download"
                  onClick={props.onExport}
                >
                  Export
                </Button>
                <Show when={props.onTogglePublished}>
                  <Toggle
                    label="Published"
                    checked={props.published}
                    onChange={() => props.onTogglePublished?.()}
                  />
                </Show>
                <Show when={props.onTogglePinned}>
                  <Tooltip label={props.pinHint ?? ""} side="bottom">
                    <Toggle
                      label="Pin to brief"
                      checked={props.pinned}
                      disabled={!props.published}
                      onChange={() => props.onTogglePinned?.()}
                    />
                  </Tooltip>
                </Show>
                <Show when={props.canEdit}>
                  <Button
                    variant="default"
                    size="sm"
                    leading="edit"
                    onClick={startEdit}
                  >
                    Edit
                  </Button>
                </Show>
                <Show when={props.menuItems.length}>
                  <Menu
                    trigger={
                      <span
                        aria-label="More actions"
                        class="flex size-8 items-center justify-center border border-line text-dim hover:bg-raised hover:text-bright"
                      >
                        <Text variant="micro">···</Text>
                      </span>
                    }
                    items={props.menuItems}
                  />
                </Show>
              </>
            }
          >
            <Button variant="ghost" size="sm" onClick={cancel}>
              Cancel
            </Button>
            <Button
              variant="primary"
              size="sm"
              leading="check"
              disabled={!dirty() || busy()}
              onClick={() => void save()}
            >
              Save
            </Button>
          </Show>
        </Row>
      </header>

      <Tabs
        items={[
          { value: "overview", label: "Overview" },
          { value: "contents", label: `Contents · ${props.files.length}` },
        ]}
        value={tab()}
        onChange={(v) => setTab(v === "contents" ? "contents" : "overview")}
      />

      <Show when={tab() === "overview"}>
        <Stack gap={4}>
          <Panel label="Description">
            <Text variant="body">{props.description}</Text>
          </Panel>
          <Panel label="Properties">
            <Stack gap={2}>
              <For each={props.properties(editing())}>
                {(p) => (
                  <div class="grid grid-cols-[10rem_minmax(0,1fr)] items-baseline gap-3">
                    <Text variant="meta" tone="dim">
                      {p.label}
                    </Text>
                    <div class="min-w-0 break-words font-mono text-code text-bright">
                      {p.value}
                    </div>
                  </div>
                )}
              </For>
            </Stack>
          </Panel>
        </Stack>
      </Show>

      <Show when={tab() === "contents"}>
        <LibraryFileBrowser
          files={props.files}
          mode={mode()}
          onMode={setMode}
          editing={editing()}
          drafts={drafts}
          onDraft={(path, text) => setDrafts(path, text)}
          onLoaded={(path, text) => setSaved(path, text)}
          version={version()}
          error={error()}
          onDeleteFile={
            props.onDeleteFile ? (p) => void deleteFile(p) : undefined
          }
          onAddFile={props.onAddFile ? addFile : undefined}
        />
      </Show>
    </Stack>
  );
}
