import {
  createEffect,
  createSignal,
  For,
  Show,
  Suspense,
  type Accessor,
  type JSX,
} from "solid-js";
import {
  Button,
  Drawer,
  EmptyState,
  Input,
  InstrumentBand,
  ListRow,
  LoadingText,
  PageHeader,
  Panel,
  Row,
  Stack,
  StatusFlag,
  Text,
  Textarea,
  toast,
} from "~/ui";
import { relativeTime } from "~/lib/format";
import {
  markMessage,
  replyToMessage,
  sendMessage,
  useEmailAccounts,
  useEmailFolders,
  useEmailMessage,
  useEmailMessages,
  useEmailSpamCount,
  useReplySuggestions,
} from "../data";
import type { EmailAccount, EmailFolder } from "../model";

const urgencyStatus = {
  low: "idle",
  normal: "idle",
  high: "alert",
} as const;

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

// ─── Account / Folder Rail ────────────────────────────────────────────────────

interface AccountFolderRailProps {
  accounts: Accessor<EmailAccount[] | undefined>;
  folders: Accessor<EmailFolder[] | undefined>;
  selectedAccountId: Accessor<string>;
  selectedFolderId: Accessor<string>;
  onAccountSelect: (acc: EmailAccount) => void;
  onFolderSelect: (folder: EmailFolder) => void;
}

function AccountFolderRail(props: AccountFolderRailProps): JSX.Element {
  const accountFolders = () =>
    (props.folders() ?? []).filter(
      (f) => f.accountId === props.selectedAccountId(),
    );

  return (
    <Stack gap={4}>
      <Panel label="Accounts" flush>
        <Suspense
          fallback={
            <div class="p-3">
              <LoadingText />
            </div>
          }
        >
          <For each={props.accounts()}>
            {(acc) => (
              <ListRow
                label={acc.name}
                leading="mail"
                selected={acc.id === props.selectedAccountId()}
                onClick={() => props.onAccountSelect(acc)}
                right={
                  <Text variant="micro" tone="dim">
                    {acc.provider}
                  </Text>
                }
              />
            )}
          </For>
        </Suspense>
      </Panel>

      <Panel label="Folders" flush>
        <Suspense
          fallback={
            <div class="p-3">
              <LoadingText />
            </div>
          }
        >
          <For each={accountFolders()}>
            {(folder) => (
              <ListRow
                label={folder.name}
                selected={folder.id === props.selectedFolderId()}
                onClick={() => props.onFolderSelect(folder)}
                right={
                  <Show when={folder.count > 0}>
                    <Text variant="micro" tone="bright">
                      {folder.count}
                    </Text>
                  </Show>
                }
              />
            )}
          </For>
        </Suspense>
      </Panel>
    </Stack>
  );
}

// ─── Email Inbox Screen ───────────────────────────────────────────────────────

export function EmailInboxScreen(): JSX.Element {
  const [selectedAccountId, setSelectedAccountId] = createSignal("");
  const [selectedFolderId, setSelectedFolderId] = createSignal("");
  const [selectedMessageId, setSelectedMessageId] = createSignal<string | null>(
    null,
  );

  const accounts = useEmailAccounts();
  const folders = useEmailFolders(selectedAccountId);
  const messages = useEmailMessages(selectedAccountId, selectedFolderId);
  const spamCount = useEmailSpamCount(selectedAccountId);
  // The listing carries no body — opening a message fetches it (and its EMAIL-4 split).
  const openMessage = useEmailMessage(selectedMessageId);
  const replySuggestions = useReplySuggestions(selectedMessageId);

  // Nothing is selected until the backend says what exists. Land on the first account
  // and its inbox once they arrive, and re-land whenever the account changes.
  createEffect(() => {
    const first = accounts()?.[0];
    if (first && !selectedAccountId()) setSelectedAccountId(first.id);
  });
  createEffect(() => {
    const available = folders() ?? [];
    if (!available.length) return;
    if (available.some((f) => f.id === selectedFolderId())) return;
    setSelectedFolderId(available[0].id);
  });

  // Compose drawer state
  const [composeOpen, setComposeOpen] = createSignal(false);
  const [composeTo, setComposeTo] = createSignal("");
  const [composeSubject, setComposeSubject] = createSignal("");
  const [composeBody, setComposeBody] = createSignal("");
  const [toError, setToError] = createSignal("");
  // Set when the drawer was opened from a message, so SEND threads the reply instead of
  // starting a new conversation.
  const [replyToId, setReplyToId] = createSignal<string | null>(null);

  // Draft recovery: show banner when drawer closes with non-empty fields
  const [hasDraft, setHasDraft] = createSignal(false);

  // Mobile: sidebar drawer
  const [mobileSidebarOpen, setMobileSidebarOpen] = createSignal(false);

  const hasComposeContent = () =>
    composeTo().trim() !== "" ||
    composeSubject().trim() !== "" ||
    composeBody().trim() !== "";

  function openCompose(
    to = "",
    subject = "",
    body = "",
    inReplyTo: string | null = null,
  ): void {
    setComposeTo(to);
    setComposeSubject(subject);
    setComposeBody(body);
    setToError("");
    setReplyToId(inReplyTo);
    setHasDraft(false);
    setComposeOpen(true);
  }

  function closeCompose(): void {
    if (hasComposeContent()) {
      setHasDraft(true);
    }
    setComposeOpen(false);
  }

  const [sending, setSending] = createSignal(false);

  function resetCompose(): void {
    setComposeOpen(false);
    setHasDraft(false);
    setComposeTo("");
    setComposeSubject("");
    setComposeBody("");
    setReplyToId(null);
  }

  async function handleSend(): Promise<void> {
    const to = composeTo().trim();
    if (!to) {
      setToError("Recipient required");
      return;
    }
    // A shape check for immediate feedback only — the backend re-validates and is the
    // authority on whether a message can go out.
    if (!EMAIL_RE.test(to)) {
      setToError("Invalid email address");
      return;
    }
    setToError("");
    const accountId = selectedAccountId();
    if (!accountId) {
      toast.error("No account selected");
      return;
    }
    setSending(true);
    try {
      const threadOf = replyToId();
      if (threadOf) {
        // Replying through the message keeps the provider's threading headers intact.
        await replyToMessage(threadOf, composeBody());
      } else {
        await sendMessage({
          accountId,
          to: [to],
          subject: composeSubject(),
          body: composeBody(),
        });
      }
      resetCompose();
      toast.success("Message sent");
    } catch {
      toast.error("Could not send message");
    } finally {
      setSending(false);
    }
  }

  const unreadCount = () => (messages() ?? []).filter((m) => !m.read).length;
  const highUrgencyCount = () =>
    (messages() ?? []).filter((m) => m.urgency === "high").length;

  const currentAccountAddress = () =>
    accounts()?.find((a) => a.id === selectedAccountId())?.address ?? "—";

  const currentFolderName = () =>
    (folders() ?? [])
      .find((f) => f.id === selectedFolderId())
      ?.name.toUpperCase() ?? "—";

  function openMessageById(id: string): void {
    setSelectedMessageId(id);
    // Opening a message marks it read — the backend writes it through to the provider,
    // and the listing refreshes from that outcome rather than a local guess.
    const row = (messages() ?? []).find((m) => m.id === id);
    if (row && !row.read) {
      markMessage(id, { read: true }).catch(() =>
        toast.error("Could not mark as read"),
      );
    }
  }

  function handleAccountSelect(acc: EmailAccount): void {
    setSelectedAccountId(acc.id);
    setSelectedFolderId("");
    setSelectedMessageId(null);
    setMobileSidebarOpen(false);
    toast.info(`ACCOUNT: ${acc.address}`);
  }

  function handleFolderSelect(folder: EmailFolder): void {
    setSelectedFolderId(folder.id);
    setSelectedMessageId(null);
    setMobileSidebarOpen(false);
  }

  return (
    <Stack gap={6}>
      <PageHeader
        title="Email"
        subtitle="Multi-account inbox with AI triage."
        assetId="COMM-MAIL-01.0"
        actions={
          <Row gap={2}>
            {/* Mobile: trigger to open the accounts/folders sidebar in a Drawer */}
            <Button
              variant="ghost"
              leading="mail"
              class="lg:hidden"
              onClick={() => setMobileSidebarOpen(true)}
            >
              Accounts
            </Button>
            <Button
              variant="primary"
              leading="plus"
              onClick={() => openCompose()}
            >
              Compose
            </Button>
          </Row>
        }
      />

      {/* Draft recovery banner — shown when compose drawer closes with content */}
      <Show when={hasDraft()}>
        <div class="flex items-center gap-3 border border-warn bg-surface px-4 py-2">
          <StatusFlag status="warn">Draft saved</StatusFlag>
          <Text variant="body" tone="dim" class="flex-1">
            Your unsent message was preserved.
          </Text>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setHasDraft(false);
              setComposeOpen(true);
            }}
          >
            Resume
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              resetCompose();
            }}
          >
            Discard
          </Button>
        </div>
      </Show>

      <Suspense fallback={<LoadingText label="Loading triage" />}>
        <InstrumentBand
          items={[
            {
              label: "Unread",
              value: String(unreadCount()),
              tone: unreadCount() > 0 ? "bright" : "dim",
            },
            {
              label: "High urgency",
              value: String(highUrgencyCount()),
              tone: highUrgencyCount() > 0 ? "alert" : "dim",
            },
            {
              label: "Spam flagged",
              value: String(spamCount() ?? 0),
              tone: (spamCount() ?? 0) > 0 ? "warn" : "dim",
            },
            {
              label: "Account",
              value: currentAccountAddress(),
            },
            {
              label: "Folder",
              value: currentFolderName(),
            },
          ]}
        />
      </Suspense>

      {/* Mobile: accounts/folders slide-in drawer */}
      <Drawer
        open={mobileSidebarOpen()}
        onClose={() => setMobileSidebarOpen(false)}
        title="Accounts & folders"
        side="left"
      >
        <AccountFolderRail
          accounts={accounts}
          folders={folders}
          selectedAccountId={selectedAccountId}
          selectedFolderId={selectedFolderId}
          onAccountSelect={handleAccountSelect}
          onFolderSelect={handleFolderSelect}
        />
      </Drawer>

      <div class="flex h-full min-h-0 gap-4">
        {/* Account / Folder rail — desktop only */}
        <aside class="hidden w-48 shrink-0 flex-col gap-4 lg:flex">
          <AccountFolderRail
            accounts={accounts}
            folders={folders}
            selectedAccountId={selectedAccountId}
            selectedFolderId={selectedFolderId}
            onAccountSelect={handleAccountSelect}
            onFolderSelect={handleFolderSelect}
          />
        </aside>

        {/* Message list */}
        <section class="flex min-h-0 w-72 shrink-0 flex-col">
          <Panel label="Messages" flush class="flex min-h-0 flex-1 flex-col">
            <div class="min-h-0 flex-1 overflow-y-auto">
              <Suspense
                fallback={
                  <div class="p-3">
                    <LoadingText />
                  </div>
                }
              >
                <Show
                  when={(messages() ?? []).length}
                  fallback={
                    <EmptyState
                      icon="mail"
                      message="No messages"
                      hint="This folder is empty."
                    />
                  }
                >
                  <For each={messages()}>
                    {(msg) => (
                      <button
                        type="button"
                        class="w-full text-left transition-colors hover:bg-raised"
                        classList={{
                          "bg-raised": msg.id === selectedMessageId(),
                        }}
                        onClick={() => openMessageById(msg.id)}
                      >
                        <div class="px-3 py-2">
                          <Row justify="between" align="start" gap={2}>
                            <Text
                              variant="label"
                              tone={msg.read ? "dim" : "bright"}
                              class="truncate"
                            >
                              {msg.fromName}
                            </Text>
                            <Text variant="micro" tone="dim" class="shrink-0">
                              {relativeTime(msg.receivedAt)}
                            </Text>
                          </Row>
                          <Text
                            variant="body"
                            tone={msg.read ? "dim" : "default"}
                            class="mt-0.5 truncate"
                          >
                            {msg.subject}
                          </Text>
                          <Text
                            variant="micro"
                            tone="dim"
                            class="mt-0.5 truncate"
                          >
                            {msg.snippet}
                          </Text>
                          <Row gap={1} wrap class="mt-1.5">
                            <Show when={msg.urgency === "high"}>
                              <StatusFlag status="alert">Urgent</StatusFlag>
                            </Show>
                            <Show when={msg.spam}>
                              <StatusFlag status="warn">Spam</StatusFlag>
                            </Show>
                            <For each={msg.tags.slice(0, 2)}>
                              {(tag) => (
                                <StatusFlag status="idle">
                                  {tag.toUpperCase()}
                                </StatusFlag>
                              )}
                            </For>
                          </Row>
                        </div>
                      </button>
                    )}
                  </For>
                </Show>
              </Suspense>
            </div>
          </Panel>
        </section>

        {/* Reading pane */}
        <section class="flex min-h-0 min-w-0 flex-1 flex-col gap-4">
          <Show
            when={openMessage()}
            fallback={
              <div class="flex flex-1 items-center justify-center">
                <EmptyState
                  icon="mail"
                  message="No message selected"
                  hint="Select a message from the list."
                />
              </div>
            }
          >
            {(msg) => (
              <>
                <Panel
                  label="Message"
                  meta={
                    <StatusFlag status={urgencyStatus[msg().urgency]}>
                      {msg().urgency.toUpperCase()}
                    </StatusFlag>
                  }
                >
                  <Stack gap={3}>
                    <div class="pb-3">
                      <Text variant="readout" tone="bright">
                        {msg().subject}
                      </Text>
                      <Row gap={2} align="center" class="mt-1">
                        <Text variant="micro" tone="dim">
                          From
                        </Text>
                        <Text variant="label" tone="default">
                          {msg().fromName}
                        </Text>
                        <Text variant="micro" tone="dim">
                          {msg().from}
                        </Text>
                        <Text variant="micro" tone="dim" class="ml-auto">
                          {relativeTime(msg().receivedAt)}
                        </Text>
                      </Row>
                    </div>
                    <Text
                      variant="body"
                      tone="default"
                      class="whitespace-pre-wrap"
                    >
                      {msg().body}
                    </Text>
                  </Stack>
                </Panel>

                <Panel label="AI summary" state="active">
                  <Text variant="body" tone="default">
                    {msg().summary}
                  </Text>
                </Panel>

                <Panel label="Suggested replies">
                  <Suspense fallback={<LoadingText />}>
                    <Row gap={2} wrap>
                      <For each={replySuggestions()}>
                        {(suggestion) => (
                          <Button
                            variant="default"
                            size="sm"
                            onClick={() => {
                              openCompose(
                                msg().from,
                                `Re: ${msg().subject}`,
                                suggestion.body,
                                msg().id,
                              );
                            }}
                          >
                            {suggestion.label}
                          </Button>
                        )}
                      </For>
                      <Button
                        variant="ghost"
                        size="sm"
                        leading="edit"
                        onClick={() => {
                          openCompose(
                            msg().from,
                            `Re: ${msg().subject}`,
                            "",
                            msg().id,
                          );
                        }}
                      >
                        Compose reply
                      </Button>
                    </Row>
                  </Suspense>
                </Panel>
              </>
            )}
          </Show>
        </section>
      </div>

      {/* Compose drawer */}
      <Drawer
        open={composeOpen()}
        onClose={closeCompose}
        title="Compose message"
        side="right"
        footer={
          <Row gap={2}>
            <Button variant="ghost" onClick={closeCompose}>
              Cancel
            </Button>
            <Button
              variant="primary"
              leading="send"
              disabled={sending()}
              onClick={() => void handleSend()}
            >
              {sending() ? "Sending…" : "Send"}
            </Button>
          </Row>
        }
      >
        <Stack gap={4}>
          <Input
            label="To"
            value={composeTo()}
            onInput={(e) => {
              setComposeTo(e.currentTarget.value);
              if (toError()) setToError("");
            }}
            placeholder="recipient@example.com"
            invalid={toError() !== ""}
            hint={toError() || undefined}
          />
          <Input
            label="Subject"
            value={composeSubject()}
            onInput={(e) => setComposeSubject(e.currentTarget.value)}
            placeholder="Subject line"
          />
          <Textarea
            label="Body"
            rows={12}
            value={composeBody()}
            onInput={(e) => setComposeBody(e.currentTarget.value)}
          />
        </Stack>
      </Drawer>
    </Stack>
  );
}
