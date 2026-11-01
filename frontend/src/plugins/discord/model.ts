export interface DiscordStatus {
  /** A bot token is stored. */
  configured: boolean;
  connected: boolean;
  botUser: string | null;
  error: string | null;
  allowedUsers: string[];
  allowedChannels: string[];
  approvalsToDm: boolean;
}

export type DiscordConfig = Pick<
  DiscordStatus,
  "allowedUsers" | "allowedChannels" | "approvalsToDm"
>;

export interface ChannelLink {
  id: string;
  address: string;
  conversationId: string;
  createdAt: string;
}
