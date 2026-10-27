import { createSignal, For, Show, Suspense, type JSX } from "solid-js";
import {
  Button,
  confirm,
  EmptyState,
  ListToolbar,
  LoadingText,
  Stack,
  StatusFlag,
  Text,
  toast,
} from "~/ui";
import { SettingsPageHeader } from "~/app/settings-dialog/SettingsPageHeader";
import { createInFlight } from "~/lib/inFlight";
import { createListView } from "~/lib/list";
import {
  connectMcpServer,
  deleteMcpServer,
  mcpErrorMessage,
  setMcpCredentials,
  setMcpToolPolicy,
  useMcpServers,
} from "../data";
import { McpAuthDrawer } from "../components/McpAuthDrawer";
import { McpServerCard } from "../components/McpServerCard";
import { RegisterServerDialog } from "../components/RegisterServerDialog";
import type { McpAuthCredentials, McpServer } from "../model";

/** Above this many servers the list stops being scannable and earns a search
 *  field; below it, a toolbar over three cards is chrome asking to be read. */
const TOOLBAR_THRESHOLD = 5;

/**
 * MCP connections — the external tool servers the agent can reach, and the two
 * decisions the operator makes about each of their tools (offered at all, and
 * allowed to run without asking).
 *
 * **A settings section, not a page.** It used to be a rail pin, which put a
 * permanent row beside the work for a surface you visit when you add a server
 * and then don't visit again. Registering a server is configuration, so it sits
 * with the other connections in the dialog.
 *
 * Orchestration only: the card, the register dialog, and the auth drawer each own
 * their own presentation, and every verdict rendered — connected or not, what a
 * failure was — is the backend's, relayed verbatim.
 */
export function McpScreen(): JSX.Element {
  const servers = useMcpServers();
  const [registerOpen, setRegisterOpen] = createSignal(false);
  // Keyed by server, because dialling is per-server and the screen outlives any one
  // attempt: a single id slot let the first server's cleanup clear the second's
  // "Connecting…" and re-offer its Retry while it was still mid-handshake.
  const connecting = createInFlight();
  const [authTarget, setAuthTarget] = createSignal<McpServer | null>(null);

  async function setToolPolicy(
    serverId: string,
    toolName: string,
    patch: { enabled?: boolean; trusted?: boolean },
  ) {
    try {
      await setMcpToolPolicy(serverId, toolName, patch);
      if (patch.trusted !== undefined) {
        toast.success(
          patch.trusted
            ? `"${toolName}" will run without asking.`
            : `"${toolName}" will ask for approval again.`,
        );
      }
    } catch (err) {
      toast.error(mcpErrorMessage(err, `Could not update "${toolName}".`));
    }
  }

  async function retryConnection(server: McpServer) {
    await connecting.run(server.id, async () => {
      try {
        // A refused connection is a 200 carrying the reason, not a thrown error —
        // so the outcome is read off the server that comes back.
        const updated = await connectMcpServer(server.id);
        if (updated.status === "connected") {
          toast.success(
            `"${updated.name}" connected — ${updated.tools.length} tools discovered.`,
          );
        } else {
          toast.error(
            updated.errorMessage ?? `Could not connect to "${updated.name}".`,
          );
        }
      } catch (err) {
        toast.error(mcpErrorMessage(err, `Could not reach "${server.name}".`));
      }
    });
  }

  async function saveCredentials(serverId: string, creds: McpAuthCredentials) {
    try {
      const updated = await setMcpCredentials(serverId, creds);
      toast.success(`Auth configured for "${updated.name}".`);
    } catch (err) {
      toast.error(mcpErrorMessage(err, "Could not save the credentials."));
    }
  }

  async function deleteServer(server: McpServer) {
    const ok = await confirm({
      title: `Delete server "${server.name}"?`,
      detail: "The agent will lose access to its tools. This cannot be undone.",
      confirmLabel: "Delete",
      cancelLabel: "Cancel",
      tone: "alert",
    });
    if (!ok) return;
    try {
      await deleteMcpServer(server.id);
      toast.success(`"${server.name}" removed.`);
    } catch (err) {
      toast.error(mcpErrorMessage(err, `Could not remove "${server.name}".`));
    }
  }

  const view = createListView({
    source: () => servers() ?? [],
    search: (s) => s.name,
    sorts: {
      name: { label: "Name", compare: (a, b) => a.name.localeCompare(b.name) },
      status: {
        label: "Status",
        compare: (a, b) => a.status.localeCompare(b.status),
      },
    },
    initialSort: "name",
  });

  const connectedCount = () =>
    (servers() ?? []).filter((s) => s.status === "connected").length;

  return (
    <Stack gap={6}>
      <SettingsPageHeader
        section="mcp"
        status={
          <StatusFlag
            status="nominal"
            dot
          >{`${connectedCount()} CONNECTED`}</StatusFlag>
        }
        actions={
          <Button
            variant="default"
            leading="plus"
            onClick={() => setRegisterOpen(true)}
          >
            Register
          </Button>
        }
      />

      <Suspense fallback={<LoadingText label="Loading servers" />}>
        <Show
          when={(servers() ?? []).length}
          fallback={
            <EmptyState
              icon="plug"
              message="No servers"
              hint="Model Context Protocol (MCP) lets the agent call external tools — file access, search, custom APIs — exposed by a server. Register one to get started."
              action={
                <Button onClick={() => setRegisterOpen(true)} leading="plus">
                  Register server
                </Button>
              }
            />
          }
        >
          <Stack gap={4}>
            <Text variant="caption" tone="dim">
              MCP servers expose tools the agent can call. Every one of them
              asks for your approval before it runs until you trust it — one
              tool at a time, never a whole server.
            </Text>
            <Show when={view.total() > TOOLBAR_THRESHOLD}>
              <ListToolbar
                query={view.query()}
                onQueryChange={view.setQuery}
                placeholder="Search servers…"
                sortKey={view.sortKey()}
                sortOptions={view.sortOptions}
                onSortChange={view.setSort}
                dir={view.dir()}
                onToggleDir={view.toggleDir}
                count={view.count()}
                total={view.total()}
              />
            </Show>
            <Show
              when={view.items().length}
              fallback={
                <EmptyState
                  icon="search"
                  message="No matches"
                  hint="No servers match your search."
                />
              }
            >
              <For each={view.items()}>
                {(srv) => (
                  <McpServerCard
                    server={srv}
                    busy={connecting.has(srv.id)}
                    onSetToolPolicy={setToolPolicy}
                    onRetry={(s) => void retryConnection(s)}
                    onConfigureAuth={(s) => setAuthTarget(s)}
                    onDelete={(s) => void deleteServer(s)}
                  />
                )}
              </For>
            </Show>
          </Stack>
        </Show>
      </Suspense>

      <RegisterServerDialog
        open={registerOpen()}
        onClose={() => setRegisterOpen(false)}
      />

      <McpAuthDrawer
        server={authTarget()}
        onClose={() => setAuthTarget(null)}
        onSave={(id, creds) => void saveCredentials(id, creds)}
      />
    </Stack>
  );
}
