import { createResource, createSignal, type Resource } from "solid-js";
import { api } from "~/lib/api";
import type { ChannelLink, DiscordConfig, DiscordStatus } from "./model";

interface StatusDto {
  configured: boolean;
  connected: boolean;
  bot_user: string | null;
  error: string | null;
  allowed_users: string[];
  allowed_channels: string[];
  approvals_to_dm: boolean;
}

interface LinkDto {
  id: string;
  channel: string;
  address: string;
  conversation_id: string;
  created_at: string;
}

function toStatus(dto: StatusDto): DiscordStatus {
  return {
    configured: dto.configured,
    connected: dto.connected,
    botUser: dto.bot_user,
    error: dto.error,
    allowedUsers: dto.allowed_users,
    allowedChannels: dto.allowed_channels,
    approvalsToDm: dto.approvals_to_dm,
  };
}

const [statusTick, setStatusTick] = createSignal(0);
const [linksTick, setLinksTick] = createSignal(0);

export function useDiscordStatus(): Resource<DiscordStatus> {
  const [data] = createResource(statusTick, async () =>
    toStatus(await api.get<StatusDto>("/discord/status")),
  );
  return data;
}

/** Re-read the connection state, which changes on its own as the bot connects. */
export function refreshDiscordStatus(): void {
  setStatusTick((n) => n + 1);
}

export async function saveDiscordConfig(config: DiscordConfig): Promise<void> {
  await api.put("/discord/config", {
    allowed_users: config.allowedUsers,
    allowed_channels: config.allowedChannels,
    approvals_to_dm: config.approvalsToDm,
  });
  refreshDiscordStatus();
}

export function useDiscordLinks(): Resource<ChannelLink[]> {
  const [data] = createResource(linksTick, async () => {
    const rows = await api.get<LinkDto[]>("/channels/links");
    return rows
      .filter((row) => row.channel === "discord")
      .map((row) => ({
        id: row.id,
        address: row.address,
        conversationId: row.conversation_id,
        createdAt: row.created_at,
      }));
  });
  return data;
}

export async function unlinkDiscordChat(id: string): Promise<void> {
  await api.del(`/channels/links/${id}`);
  setLinksTick((n) => n + 1);
}
