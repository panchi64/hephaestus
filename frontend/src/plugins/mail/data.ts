import { createResource, createSignal, type Resource } from "solid-js";
import { api } from "~/lib/api";
import type {
  EmailAccount,
  EmailFolder,
  EmailMessage,
  EmailUrgency,
  ReplySuggestion,
} from "./model";

/* ── Backend DTOs → seam types ────────────────────────────────────────────── */

interface AccountOut {
  id: string;
  name: string;
  address: string;
  provider: string;
  authKind: string;
  enabled: boolean;
  status: string;
  errorDetail: string | null;
  lastSyncedAt: string | null;
}

interface FolderOut {
  id: string;
  accountId: string;
  name: string;
  role: string;
  count: number;
}

interface MessageOut {
  id: string;
  accountId: string;
  folderId: string;
  from: string;
  fromName: string;
  to: string[];
  subject: string;
  snippet: string;
  body: string;
  receivedAt: string;
  read: boolean;
  flagged: boolean;
  urgency: string;
  tags: string[];
  spam: boolean;
  summary: string;
  replyText: string;
  quotedText: string | null;
  signature: string | null;
}

interface SuggestionOut {
  id: string;
  label: string;
  body: string;
}

interface SendResult {
  messageId: string;
}

function toAccount(dto: AccountOut): EmailAccount {
  return {
    id: dto.id,
    name: dto.name,
    address: dto.address,
    provider: dto.provider,
  };
}

function toFolder(dto: FolderOut): EmailFolder {
  return {
    id: dto.id,
    accountId: dto.accountId,
    name: dto.name,
    count: dto.count,
  };
}

/**
 * Urgency is a backend verdict; this only narrows the wire string to the union the
 * screen renders, falling back to `normal` for a value it doesn't know.
 */
function toUrgency(value: string): EmailUrgency {
  return value === "high" || value === "low" ? value : "normal";
}

function toMessage(dto: MessageOut): EmailMessage {
  return {
    id: dto.id,
    accountId: dto.accountId,
    folderId: dto.folderId,
    from: dto.from,
    fromName: dto.fromName,
    to: dto.to,
    subject: dto.subject,
    snippet: dto.snippet,
    // The sender's own prose when the backend could split it from the quoted thread
    // (EMAIL-4); the whole body otherwise.
    body: dto.replyText || dto.body,
    receivedAt: dto.receivedAt,
    read: dto.read,
    urgency: toUrgency(dto.urgency),
    tags: dto.tags,
    spam: dto.spam,
    summary: dto.summary,
  };
}

/* ── Accounts ─────────────────────────────────────────────────────────────── */

async function fetchAccounts(): Promise<EmailAccount[]> {
  const rows = await api.get<AccountOut[]>("/mail/accounts");
  return rows.map(toAccount);
}

export function useEmailAccounts(): Resource<EmailAccount[]> {
  const [data] = createResource(fetchAccounts);
  return data;
}

/* ── Folders ──────────────────────────────────────────────────────────────── */

async function fetchFolders(accountId: string): Promise<EmailFolder[]> {
  if (!accountId) return [];
  const rows = await api.get<FolderOut[]>(
    `/mail/accounts/${accountId}/folders`,
  );
  return rows.map(toFolder);
}

export function useEmailFolders(
  accountId: () => string,
): Resource<EmailFolder[]> {
  const [data] = createResource(accountId, fetchFolders);
  return data;
}

/* ── Messages ─────────────────────────────────────────────────────────────── */

const [messageTick, setMessageTick] = createSignal(0);

async function fetchMessages(
  accountId: string,
  folderId: string,
): Promise<EmailMessage[]> {
  if (!accountId) return [];
  const params = new URLSearchParams({ account_id: accountId });
  if (folderId) params.set("folder", folderId);
  const rows = await api.get<MessageOut[]>(`/mail/messages?${params}`);
  return rows.map(toMessage);
}

/**
 * The listing for one account+folder. Bodies are absent here — the backend serves the
 * list from its inbox cache (EMAIL-5) and fetches a body only when one is opened.
 */
export function useEmailMessages(
  accountId: () => string,
  folderId: () => string,
): Resource<EmailMessage[]> {
  const [data] = createResource(
    () => [accountId(), folderId(), messageTick()] as const,
    ([account, folder]) => fetchMessages(account, folder),
  );
  return data;
}

/** Invalidate the listing after a mutation. */
export function refreshEmailMessages(): void {
  setMessageTick((n) => n + 1);
}

/**
 * How much of this account the backend flagged as spam (EMAIL-2). Its own request
 * because the inbox listing deliberately excludes spam — the count is a triage readout,
 * not a filter the screen applies.
 */
export function useEmailSpamCount(accountId: () => string): Resource<number> {
  const [data] = createResource(
    () => [accountId(), messageTick()] as const,
    async ([account]) => {
      if (!account) return 0;
      const params = new URLSearchParams({
        account_id: account,
        include_spam: "true",
      });
      const rows = await api.get<MessageOut[]>(`/mail/messages?${params}`);
      return rows.filter((m) => m.spam).length;
    },
  );
  return data;
}

/** One message with its body, fetched from the provider on first open, then cached. */
export function useEmailMessage(
  messageId: () => string | null,
): Resource<EmailMessage | null> {
  const [data] = createResource(
    () => [messageId(), messageTick()] as const,
    async ([id]) => {
      if (!id) return null;
      return toMessage(await api.get<MessageOut>(`/mail/messages/${id}`));
    },
  );
  return data;
}

/* ── Reply suggestions (EMAIL-3) ──────────────────────────────────────────── */

/**
 * Drafts written in the operator's own learned voice. An empty list is a real answer —
 * a workspace with no utility model bound simply has none to offer.
 */
export function useReplySuggestions(
  messageId: () => string | null,
): Resource<ReplySuggestion[]> {
  const [data] = createResource(messageId, async (id) => {
    if (!id) return [];
    return api.get<SuggestionOut[]>(`/mail/messages/${id}/suggestions`);
  });
  return data;
}

/* ── Mutations ────────────────────────────────────────────────────────────── */

export async function markMessage(
  id: string,
  patch: { read?: boolean; flagged?: boolean },
): Promise<void> {
  await api.patch(`/mail/messages/${id}`, patch);
  refreshEmailMessages();
}

export async function sendMessage(input: {
  accountId: string;
  to: string[];
  subject: string;
  body: string;
  cc?: string[];
}): Promise<string> {
  const result = await api.post<SendResult>("/mail/send", {
    account_id: input.accountId,
    to: input.to,
    subject: input.subject,
    body: input.body,
    cc: input.cc,
  });
  refreshEmailMessages();
  return result.messageId;
}

export async function replyToMessage(
  messageId: string,
  body: string,
  replyAll = false,
): Promise<string> {
  const result = await api.post<SendResult>(
    `/mail/messages/${messageId}/reply`,
    { body, reply_all: replyAll },
  );
  refreshEmailMessages();
  return result.messageId;
}
