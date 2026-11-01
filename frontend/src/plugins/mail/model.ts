/** Email feature data contracts. */

export type EmailUrgency = "low" | "normal" | "high";

export interface EmailAccount {
  id: string;
  name: string;
  address: string;
  provider: string;
}

export interface EmailFolder {
  id: string;
  accountId: string;
  name: string;
  count: number;
}

export interface EmailMessage {
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
  urgency: EmailUrgency;
  tags: string[];
  spam: boolean;
  summary: string;
}

export interface ReplySuggestion {
  id: string;
  label: string;
  body: string;
}
