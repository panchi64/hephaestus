import { createSignal, For, Show, type JSX } from "solid-js";
import {
  Button,
  ExpandableText,
  InfoHint,
  ListRow,
  Menu,
  Panel,
  Row,
  Stack,
  StatusFlag,
  Text,
  Toggle,
  Tooltip,
  type Status,
} from "~/ui";
import { parseInstant } from "~/lib/format";
import { TRANSPORT_HINT, TRUST_HINT } from "../hints";
import type { McpServer, McpStatus } from "../model";

const mcpStatusFlag: Record<McpStatus, Status> = {
  connected: "nominal",
  error: "alert",
  disconnected: "idle",
};

/** Format an ISO timestamp to a short readable label. */
function formatErrorTime(iso: string): string {
  try {
    return new Date(parseInstant(iso)).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

export interface McpServerCardProps {
  server: McpServer;
  /** True while this server's connect call is in flight. */
  busy: boolean;
  onSetToolPolicy: (
    serverId: string,
    toolName: string,
    patch: { enabled?: boolean; trusted?: boolean },
  ) => void;
  onRetry: (server: McpServer) => void;
  onConfigureAuth: (server: McpServer) => void;
  onDelete: (server: McpServer) => void;
}

/** One registered server: how it is reached, whether it answered, and the tools
 *  it exposes with their two per-tool decisions. Presentational — every action is
 *  the caller's, and every verdict shown here is the backend's. */
export function McpServerCard(props: McpServerCardProps): JSX.Element {
  const [expanded, setExpanded] = createSignal(false);
  const enabledCount = () => props.server.tools.filter((t) => t.enabled).length;
  const trustedCount = () => props.server.tools.filter((t) => t.trusted).length;
  const hasTools = () => props.server.tools.length > 0;

  const isError = () => props.server.status === "error";
  const isDisconnected = () => props.server.status === "disconnected";
  const needsAttention = () => isError() || isDisconnected();

  const errorTooltip = () => {
    const msg = props.server.errorMessage ?? "Unknown error";
    const at = props.server.errorAt
      ? ` — ${formatErrorTime(props.server.errorAt)}`
      : "";
    return `${msg}${at}`;
  };

  return (
    <Panel
      label={props.server.name}
      state={isError() ? "alert" : "default"}
      meta={
        <Row gap={2} align="center">
          <Show when={props.server.authRequired}>
            <StatusFlag status="info">Auth</StatusFlag>
          </Show>

          {/* Error status with tooltip showing diagnostics */}
          <Show
            when={isError()}
            fallback={
              <StatusFlag status={mcpStatusFlag[props.server.status]}>
                {props.server.status.toUpperCase()}
              </StatusFlag>
            }
          >
            <Tooltip label={errorTooltip()} side="left">
              <StatusFlag status="alert">Error</StatusFlag>
            </Tooltip>
          </Show>

          {/* Retry button for error or disconnected servers */}
          <Show when={needsAttention()}>
            <Button
              size="sm"
              variant="ghost"
              leading="refresh"
              pending={props.busy}
              onClick={() => props.onRetry(props.server)}
            >
              {props.busy ? "Connecting…" : "Retry"}
            </Button>
          </Show>

          {/* Configure auth button for auth-required servers */}
          <Show when={props.server.authRequired}>
            <Button
              size="sm"
              variant="ghost"
              leading="key"
              onClick={() => props.onConfigureAuth(props.server)}
            >
              {props.server.hasCredentials ? "Update auth" : "Configure auth"}
            </Button>
          </Show>

          <Show
            when={hasTools()}
            fallback={
              <Tooltip label="This server exposes no tools to the agent.">
                <Button size="sm" variant="ghost" disabled>
                  0 tools
                </Button>
              </Tooltip>
            }
          >
            <Button
              size="sm"
              variant="ghost"
              trailing={expanded() ? "chevron-down" : "chevron-right"}
              onClick={() => setExpanded((v) => !v)}
            >
              {props.server.tools.length} TOOLS
            </Button>
          </Show>

          <Menu
            trigger={
              <span class="px-1 text-dim hover:text-bright">
                <Text variant="micro">···</Text>
              </span>
            }
            items={[
              {
                label: "Delete server",
                icon: "trash",
                danger: true,
                onSelect: () => props.onDelete(props.server),
              },
            ]}
          />
        </Row>
      }
      flush={expanded() && hasTools()}
    >
      <Show when={!expanded()}>
        <Stack gap={2}>
          <Row gap={2} align="center">
            <Text variant="micro" tone="dim">
              Transport
            </Text>
            <Text variant="micro" tone="bright">
              {props.server.transport.toUpperCase()}
            </Text>
            <InfoHint label={TRANSPORT_HINT} size={12} />
          </Row>
          <Row gap={2} align="center">
            <Text variant="micro" tone="dim">
              Endpoint
            </Text>
            <Text variant="micro" tone="bright">
              {props.server.url}
            </Text>
          </Row>
          <Row gap={2} align="center">
            <Text variant="micro" tone="dim">
              Tools enabled
            </Text>
            <Text variant="micro" tone="nominal">
              {enabledCount()} / {props.server.tools.length}
            </Text>
            <Text variant="micro" tone="dim">
              ·
            </Text>
            <Text variant="micro" tone="dim">
              Trusted
            </Text>
            <Text variant="micro" tone="bright">
              {trustedCount()}
            </Text>
            <InfoHint label={TRUST_HINT} size={12} />
          </Row>

          {/* Inline error detail for error-state servers */}
          <Show when={isError() && props.server.errorMessage}>
            <Row gap={2} align="center">
              <Text variant="micro" tone="dim">
                Last error
              </Text>
              <Text variant="micro" tone="alert">
                {props.server.errorMessage}
              </Text>
            </Row>
          </Show>
        </Stack>
      </Show>

      <Show when={expanded() && hasTools()}>
        <For each={props.server.tools}>
          {(tool) => (
            <ListRow
              label={tool.name}
              leading="code"
              right={
                <Row gap={3} align="center">
                  <ExpandableText
                    text={tool.description}
                    limit={120}
                    variant="caption"
                    tone="dim"
                    class="max-w-xs"
                  />
                  <Tooltip label={TRUST_HINT} side="left">
                    <Row gap={2} align="center">
                      <Text
                        variant="micro"
                        tone={tool.trusted ? "bright" : "dim"}
                      >
                        {tool.trusted ? "Trusted" : "Asks first"}
                      </Text>
                      <Toggle
                        checked={tool.trusted}
                        disabled={!tool.enabled}
                        onChange={(v) =>
                          props.onSetToolPolicy(props.server.id, tool.name, {
                            trusted: v,
                          })
                        }
                      />
                    </Row>
                  </Tooltip>
                  <Tooltip
                    label="Whether this tool is offered to the agent at all."
                    side="left"
                  >
                    <Row gap={2} align="center">
                      <Text variant="micro" tone="dim">
                        Enabled
                      </Text>
                      <Toggle
                        checked={tool.enabled}
                        onChange={(v) =>
                          props.onSetToolPolicy(props.server.id, tool.name, {
                            enabled: v,
                          })
                        }
                      />
                    </Row>
                  </Tooltip>
                </Row>
              }
            />
          )}
        </For>
        <div class="px-3 py-2">
          <Row gap={2} align="center">
            <Text variant="micro" tone="dim">
              Transport
            </Text>
            <Text variant="micro" tone="bright">
              {props.server.transport.toUpperCase()}
            </Text>
            <InfoHint label={TRANSPORT_HINT} size={12} />
            <Text variant="micro" tone="dim">
              ·
            </Text>
            <Text variant="micro" tone="bright">
              {props.server.url}
            </Text>
          </Row>
        </div>
      </Show>
    </Panel>
  );
}
