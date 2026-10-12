import { createSignal, Show, Suspense, type JSX } from "solid-js";
import {
  RevisionHistory,
  refreshRevisions,
  revisionsBase,
} from "~/features/revisions";
import { settled } from "~/lib/resource";
import { Button, EmptyState, LoadingText, Segmented } from "~/ui";
import { LibraryDetail, type LibraryFileSource } from "~/features/library";
import {
  pluginErrorMessage,
  refreshPluginDetail,
  usePluginDetail,
  usePluginFiles,
} from "../data";
import {
  binaryNote,
  canPublish,
  contributionParts,
  hasPendingDraft,
  isAgentDrafted,
  type Plugin,
  type PluginVersion,
} from "../model";
import {
  deleteWithConfirm,
  discardWithConfirm,
  exportWithToast,
  publishWithConfirm,
  unpublishWithToast,
} from "../actions";
import { PluginOverview } from "../components/PluginOverview";
import { PluginSecrets } from "../components/PluginSecrets";
import { PluginStateFlags } from "../components/PluginStateFlags";

function metaOf(p: Plugin): string {
  const parts = contributionParts(p);
  if (isAgentDrafted(p)) parts.push("drafted by an agent");
  if (hasPendingDraft(p)) parts.push("review before publishing");
  return parts.join(" · ");
}

/** A plugin's review screen: what it does and what publishing would change, its
 *  files, its history, and the secrets it uses. Read-only — agents write the
 *  files, the operator decides. */
export function PluginDetailScreen(props: {
  id: string;
  onBack: () => void;
}): JSX.Element {
  const resource = usePluginDetail(() => props.id);
  const plugin = () => settled(resource) ?? undefined;
  const loadError = (): string | null =>
    resource.error
      ? pluginErrorMessage(resource.error, "Could not load this plugin.")
      : null;

  const [picked, setPicked] = createSignal<"draft" | "active" | null>(null);
  const shown = (): PluginVersion => {
    const p = plugin();
    const want = picked();
    if (want === "active" && p?.active) return "active";
    if (want === "draft" && p?.draft) return "draft";
    return p?.draft ? "draft" : "active";
  };
  const files = usePluginFiles(() => props.id, shown);

  const fileSources = (): LibraryFileSource[] =>
    (settled(files) ?? []).map((f) => ({
      path: f.path,
      load: () => Promise.resolve(f.content ?? binaryNote(f)),
    }));

  function changed(): void {
    refreshRevisions();
    refreshPluginDetail();
  }

  async function publish(p: Plugin): Promise<void> {
    if (await publishWithConfirm(p)) changed();
  }

  async function unpublish(p: Plugin): Promise<void> {
    if (await unpublishWithToast(p)) changed();
  }

  async function discard(p: Plugin): Promise<void> {
    if (await discardWithConfirm(p)) props.onBack();
    else changed();
  }

  async function remove(p: Plugin): Promise<void> {
    if (await deleteWithConfirm(p)) props.onBack();
  }

  return (
    <Suspense fallback={<LoadingText label="Loading plugin" />}>
      <Show
        when={plugin()}
        fallback={
          <EmptyState
            icon="plug"
            message={loadError() ? "Plugin unavailable" : "Plugin not found"}
            hint={loadError() ?? "The requested plugin does not exist."}
            action={
              <Button
                variant="default"
                leading="chevron-left"
                onClick={props.onBack}
              >
                Back to plugins
              </Button>
            }
          />
        }
      >
        {(p) => (
          <LibraryDetail
            backLabel="Plugins"
            onBack={props.onBack}
            icon="plug"
            name={p().name}
            meta={metaOf(p())}
            flags={<PluginStateFlags plugin={p()} />}
            published={p().published}
            onExport={() => void exportWithToast(p())}
            canEdit={false}
            actions={
              <Show when={canPublish(p())}>
                <Button
                  variant="primary"
                  size="sm"
                  leading="check"
                  onClick={() => void publish(p())}
                >
                  {p().draft ? "Publish" : "Enable"}
                </Button>
              </Show>
            }
            menuItems={[
              ...(p().published
                ? [
                    {
                      label: "Disable",
                      icon: "pause" as const,
                      onSelect: () => void unpublish(p()),
                    },
                  ]
                : []),
              ...(hasPendingDraft(p())
                ? [
                    {
                      label: "Discard draft",
                      icon: "close" as const,
                      onSelect: () => void discard(p()),
                    },
                  ]
                : []),
              {
                label: "Delete",
                icon: "trash" as const,
                danger: true,
                onSelect: () => void remove(p()),
              },
            ]}
            overview={<PluginOverview plugin={p()} />}
            files={fileSources()}
            contentsHeader={
              <Show when={p().draft && p().active}>
                <Segmented
                  aria-label="Version shown"
                  fill={false}
                  value={shown() === "active" ? "active" : "draft"}
                  onChange={setPicked}
                  options={[
                    { value: "draft", label: "Draft" },
                    { value: "active", label: "Published" },
                  ]}
                />
              </Show>
            }
            history={
              <RevisionHistory
                base={revisionsBase("plugins", p().id)}
                onRestored={changed}
              />
            }
            extraTabs={[
              {
                value: "secrets",
                label: `Secrets · ${Object.keys((p().draft ?? p().active)?.secrets ?? {}).length}`,
                content: <PluginSecrets plugin={p()} />,
              },
            ]}
          />
        )}
      </Show>
    </Suspense>
  );
}
