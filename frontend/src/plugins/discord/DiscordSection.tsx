import { onCleanup, For, Show, type JSX } from "solid-js";
import { settled } from "~/lib/resource";
import {
  Button,
  LoadingText,
  Panel,
  Row,
  Stack,
  StatusFlag,
  Text,
  Toggle,
  toast,
} from "~/ui";
import { SettingsPageHeader } from "~/app/settings-dialog/SettingsPageHeader";
import {
  refreshDiscordStatus,
  saveDiscordConfig,
  unlinkDiscordChat,
  useDiscordLinks,
  useDiscordStatus,
} from "./data";
import { IdListField } from "./IdListField";
import type { DiscordConfig, DiscordStatus } from "./model";
import type { PluginSettingsProps } from "../types";

const STATUS_POLL_MS = 5_000;

function connectionFlag(status: DiscordStatus): JSX.Element {
  if (status.connected)
    return (
      <StatusFlag status="nominal">
        {status.botUser ? `CONNECTED · ${status.botUser}` : "CONNECTED"}
      </StatusFlag>
    );
  if (status.error)
    return <StatusFlag status="alert">{status.error}</StatusFlag>;
  if (!status.configured)
    return <StatusFlag status="idle">NO BOT TOKEN</StatusFlag>;
  return <StatusFlag status="warn">CONNECTING</StatusFlag>;
}

export function DiscordSection(props: PluginSettingsProps): JSX.Element {
  const status = useDiscordStatus();
  const links = useDiscordLinks();
  const poll = setInterval(refreshDiscordStatus, STATUS_POLL_MS);
  onCleanup(() => clearInterval(poll));

  const save = async (config: DiscordConfig) => {
    try {
      await saveDiscordConfig(config);
    } catch {
      toast.error("Couldn't save the Discord settings.");
    }
  };

  const unlink = async (id: string) => {
    try {
      await unlinkDiscordChat(id);
    } catch {
      toast.error("Couldn't unlink that chat.");
    }
  };

  return (
    <Stack gap={6}>
      <SettingsPageHeader section="discord" />

      <Show when={settled(status)} fallback={<LoadingText />}>
        {(current) => (
          <>
            <Panel label="Connection">
              <Row align="center" justify="between" gap={3}>
                <Stack gap={1}>
                  <Text variant="label" tone="default">
                    Bot
                  </Text>
                  <Text variant="caption" tone="dim">
                    The bot token is a service key; set it under Service keys.
                  </Text>
                </Stack>
                <Row gap={2} align="center">
                  {connectionFlag(current())}
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => props.openSection("service-keys")}
                  >
                    SET TOKEN
                  </Button>
                </Row>
              </Row>
            </Panel>

            <Panel label="Who can talk to the agent">
              <Stack gap={4}>
                <IdListField
                  label="Users"
                  hint="Discord user ids allowed to message the bot, and to approve from its buttons. Everyone else is ignored."
                  ids={current().allowedUsers}
                  onChange={(allowedUsers) =>
                    void save({ ...current(), allowedUsers })
                  }
                />
                <IdListField
                  label="Channels"
                  hint="Server channel ids the bot answers in. Direct messages need no entry."
                  ids={current().allowedChannels}
                  onChange={(allowedChannels) =>
                    void save({ ...current(), allowedChannels })
                  }
                />
                <Row align="center" justify="between" gap={3}>
                  <Stack gap={1}>
                    <Text variant="label" tone="default">
                      Send other approvals to my DMs
                    </Text>
                    <Text variant="caption" tone="dim">
                      Approval requests from chats not started on Discord arrive
                      as a direct message. Needs exactly one allowed user.
                    </Text>
                  </Stack>
                  <Toggle
                    checked={current().approvalsToDm}
                    onChange={(approvalsToDm) =>
                      void save({ ...current(), approvalsToDm })
                    }
                  />
                </Row>
              </Stack>
            </Panel>
          </>
        )}
      </Show>

      <Panel label="Linked chats" flush>
        <Show
          when={settled(links)}
          fallback={
            <div class="p-3">
              <LoadingText />
            </div>
          }
        >
          {(rows) => (
            <For
              each={rows()}
              fallback={
                <div class="p-3">
                  <Text variant="caption" tone="dim">
                    No chats are linked yet. Message the bot to start one.
                  </Text>
                </div>
              }
            >
              {(link) => (
                <Row align="center" justify="between" gap={3} class="px-3 py-2">
                  <Text variant="label" tone="bright">
                    {link.address}
                  </Text>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => void unlink(link.id)}
                  >
                    UNLINK
                  </Button>
                </Row>
              )}
            </For>
          )}
        </Show>
      </Panel>
    </Stack>
  );
}
