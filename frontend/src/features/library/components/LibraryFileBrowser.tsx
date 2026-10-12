import {
  createEffect,
  createMemo,
  createResource,
  createSignal,
  For,
  Show,
  type JSX,
} from "solid-js";
import { settled } from "~/lib/resource";
import { Icon, Input, Markdown, Panel, Text, cx } from "~/ui";
import {
  buildFileTree,
  isMarkdownPath,
  isTextPath,
  stripFrontmatter,
  type TreeNode,
} from "../model";
import { CodeEditor } from "./CodeEditor";

/** One file in the Contents pane. `load` returns its whole text. */
export interface LibraryFileSource {
  path: string;
  load: () => Promise<string>;
}

export type ViewMode = "preview" | "source";

const MODES: readonly {
  value: ViewMode;
  icon: "eye" | "code";
  label: string;
}[] = [
  { value: "preview", icon: "eye", label: "Preview" },
  { value: "source", icon: "code", label: "Source" },
];

/** The Contents tab: a file tree on the left, the selected file on the right as
 *  rendered markdown or raw text. The first file is the item's own markdown
 *  file; its preview drops the frontmatter, its source shows it.
 *
 *  While `editing`, source becomes an editor. The unsaved text of every file
 *  touched lives in `drafts`, owned by the caller, so switching files keeps
 *  each one's edits. */
export function LibraryFileBrowser(props: {
  files: LibraryFileSource[];
  mode: ViewMode;
  onMode: (mode: ViewMode) => void;
  editing: boolean;
  drafts: Record<string, string>;
  onDraft: (path: string, text: string) => void;
  /** Called with a file's saved text once it has loaded. */
  onLoaded: (path: string, text: string) => void;
  /** Bumped by the caller to reload from the backend. */
  version: number;
  error: string | null;
  onDeleteFile?: (path: string) => void;
  onAddFile?: (relpath: string) => Promise<void>;
}): JSX.Element {
  const mainPath = () => props.files[0]?.path ?? "";
  const [picked, setPicked] = createSignal("");
  const [folded, setFolded] = createSignal<ReadonlySet<string>>(new Set());
  const [adding, setAdding] = createSignal(false);
  const [newPath, setNewPath] = createSignal("");

  const selected = () =>
    props.files.some((f) => f.path === picked()) ? picked() : mainPath();
  const tree = createMemo(() =>
    buildFileTree(
      mainPath(),
      props.files.slice(1).map((f) => f.path),
    ),
  );
  const [text] = createResource(
    () => ({
      file: props.files.find((f) => f.path === selected()),
      v: props.version,
    }),
    async ({ file }) => ({
      path: file?.path ?? "",
      content: (await file?.load()) ?? "",
    }),
  );
  // A refetch keeps the previous file on screen until the next arrives, so the
  // text is only trusted for the path it was loaded for.
  const loaded = () => settled(text);
  const content = () => {
    const l = loaded();
    return l && l.path === selected() ? l.content : undefined;
  };
  createEffect(() => {
    const l = loaded();
    if (l) props.onLoaded(l.path, l.content);
  });

  const isMain = () => selected() === mainPath();
  const markdown = () => isMarkdownPath(selected());
  const editable = () => props.editing && (isMain() || isTextPath(selected()));
  const shown = () => props.drafts[selected()] ?? content() ?? "";
  const effective = (): ViewMode => (markdown() ? props.mode : "source");

  function toggleFolder(path: string): void {
    setFolded((prev) => {
      const next = new Set(prev);
      if (!next.delete(path)) next.add(path);
      return next;
    });
  }

  async function submitNew(): Promise<void> {
    const relpath = newPath().trim().replace(/^\/+/, "");
    if (!relpath || !props.onAddFile) return;
    await props.onAddFile(relpath);
    setPicked(relpath);
    setNewPath("");
    setAdding(false);
  }

  const node = (n: TreeNode, depth: number): JSX.Element => {
    const pad = { "padding-left": `${8 + depth * 14}px` };
    if (n.kind === "file") {
      return (
        <button
          type="button"
          onClick={() => setPicked(n.path)}
          style={pad}
          class={cx(
            "flex w-full items-center gap-2 py-1.5 pr-2 text-left transition-colors hover:bg-raised",
            selected() === n.path && "bg-raised ody-selected-edge",
          )}
        >
          <Icon name="file" size={14} class="shrink-0 text-dim" />
          <Text
            variant="code"
            tone={selected() === n.path ? "bright" : "default"}
            class="truncate"
          >
            {n.name}
          </Text>
        </button>
      );
    }
    const open = () => !folded().has(n.path);
    return (
      <>
        <button
          type="button"
          onClick={() => toggleFolder(n.path)}
          aria-expanded={open()}
          style={pad}
          class="flex w-full items-center gap-2 py-1.5 pr-2 text-left transition-colors hover:bg-raised"
        >
          <Icon
            name={open() ? "chevron-down" : "chevron-right"}
            size={14}
            class="shrink-0 text-dim"
          />
          <Text variant="code" tone="dim" class="truncate">
            {n.name}/
          </Text>
        </button>
        <Show when={open()}>
          <For each={n.children}>{(child) => node(child, depth + 1)}</For>
        </Show>
      </>
    );
  };

  return (
    <Panel flush>
      <div class="grid min-h-80 grid-cols-1 sm:grid-cols-[14rem_minmax(0,1fr)]">
        <div class="flex min-h-0 flex-col border-b border-line sm:border-r sm:border-b-0">
          <div class="scrollbar-thin min-h-0 flex-1 overflow-y-auto py-1">
            <For each={tree()}>{(n) => node(n, 0)}</For>
          </div>
          <Show when={props.editing && props.onAddFile}>
            <div class="border-t border-line p-2">
              <Show
                when={adding()}
                fallback={
                  <button
                    type="button"
                    onClick={() => setAdding(true)}
                    class="flex w-full items-center gap-2 px-1 py-1 text-dim transition-colors hover:text-bright"
                  >
                    <Icon name="plus" size={14} />
                    <Text variant="label">Add file</Text>
                  </button>
                }
              >
                <Input
                  aria-label="New file path"
                  placeholder="references/notes.md"
                  value={newPath()}
                  autofocus
                  onInput={(e) => setNewPath(e.currentTarget.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") void submitNew();
                    if (e.key === "Escape") {
                      e.stopPropagation();
                      setAdding(false);
                    }
                  }}
                />
              </Show>
            </div>
          </Show>
        </div>

        <div class="flex min-w-0 flex-col">
          <div class="flex items-center justify-between gap-3 border-b border-line px-3 py-1.5">
            <Text variant="code" tone="bright" class="truncate">
              /{selected()}
            </Text>
            <div class="flex shrink-0 items-center gap-2">
              <Show when={props.editing && !isMain() && props.onDeleteFile}>
                <button
                  type="button"
                  aria-label="Delete file"
                  title="Delete file"
                  onClick={() => props.onDeleteFile?.(selected())}
                  class="flex size-7 items-center justify-center text-dim transition-colors hover:bg-raised hover:text-alert"
                >
                  <Icon name="trash" size={14} />
                </button>
              </Show>
              <div class="flex items-center" role="group" aria-label="View">
                <For each={MODES}>
                  {(m) => (
                    <button
                      type="button"
                      aria-label={m.label}
                      title={m.label}
                      aria-pressed={effective() === m.value}
                      disabled={m.value === "preview" && !markdown()}
                      onClick={() => props.onMode(m.value)}
                      class={cx(
                        "flex size-7 items-center justify-center border transition-colors disabled:cursor-not-allowed disabled:opacity-40",
                        effective() === m.value
                          ? "border-bright bg-bright text-bg"
                          : "border-line text-dim hover:bg-raised hover:text-bright",
                      )}
                    >
                      <Icon name={m.icon} size={14} />
                    </button>
                  )}
                </For>
              </div>
            </div>
          </div>

          <Show when={props.error}>
            {(message) => (
              <div
                role="alert"
                class="border-b border-line bg-surface px-3 py-2"
              >
                <Text variant="micro" tone="alert">
                  {message()}
                </Text>
              </div>
            )}
          </Show>

          <div class="min-h-0 flex-1">
            <Show
              when={!text.error}
              fallback={
                <div class="p-3">
                  <Text variant="micro" tone="alert">
                    Could not load this file.
                  </Text>
                </div>
              }
            >
              <Show
                when={effective() === "preview"}
                fallback={
                  <Show
                    when={editable()}
                    fallback={
                      <pre class="scrollbar-thin max-h-[28rem] overflow-auto p-3 font-mono text-code whitespace-pre-wrap text-text">
                        {shown()}
                      </pre>
                    }
                  >
                    <div class="h-[28rem]">
                      <CodeEditor
                        label={`Edit ${selected()}`}
                        value={shown()}
                        onInput={(v) => props.onDraft(selected(), v)}
                      />
                    </div>
                  </Show>
                }
              >
                <div class="scrollbar-thin max-h-[28rem] overflow-auto p-3">
                  <Markdown dense>
                    {isMain() ? stripFrontmatter(shown()) : shown()}
                  </Markdown>
                </div>
              </Show>
            </Show>
          </div>
        </div>
      </div>
    </Panel>
  );
}
