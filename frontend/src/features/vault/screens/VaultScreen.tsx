import {
  createSignal,
  For,
  onCleanup,
  Show,
  Suspense,
  type JSX,
} from "solid-js";
import {
  Button,
  confirm,
  EmptyState,
  Icon,
  InfoHint,
  Input,
  ListToolbar,
  LoadingText,
  Menu,
  Modal,
  Panel,
  Row,
  Stack,
  StatusFlag,
  Text,
  toast,
  Tooltip,
} from "~/ui";
import { SettingsPageHeader } from "~/app/settings-dialog/SettingsPageHeader";
import { isApiError } from "~/lib/api";
import { createListView } from "~/lib/list";
import {
  configureVault,
  createVaultEntry,
  deleteVaultEntry,
  lockVault,
  unlockVault,
  useVaultEntries,
  useVaultState,
} from "../data";
import type { VaultEntry } from "../model";

/** Rough passphrase-strength label for the setup field. Presentation only — the
 *  backend neither enforces nor sees this. */
function passphraseStrength(
  pw: string,
): { label: string; status: "alert" | "warn" | "nominal" } | null {
  if (!pw) return null;
  let score = 0;
  if (pw.length >= 8) score++;
  if (pw.length >= 12) score++;
  if (/[a-z]/.test(pw) && /[A-Z]/.test(pw)) score++;
  if (/\d/.test(pw)) score++;
  if (/[^A-Za-z0-9]/.test(pw)) score++;
  if (score <= 2) return { label: "Weak", status: "alert" };
  if (score <= 3) return { label: "Fair", status: "warn" };
  return { label: "Strong", status: "nominal" };
}

function errorText(err: unknown, fallback: string): string {
  return isApiError(err) ? err.detail : fallback;
}

const EMPTY_DRAFT = { name: "", username: "", url: "", password: "" };

export function VaultScreen(): JSX.Element {
  const state = useVaultState();
  const locked = () => state()?.locked ?? true;
  const configured = () => state()?.configured ?? false;
  const entries = useVaultEntries(() => !locked());

  const timers: ReturnType<typeof setTimeout>[] = [];
  onCleanup(() => timers.forEach(clearTimeout));

  const [passphrase, setPassphrase] = createSignal("");
  const [passphraseError, setPassphraseError] = createSignal<string | null>(
    null,
  );
  const [busy, setBusy] = createSignal(false);

  // Per-entry copy-feedback state
  const [copiedId, setCopiedId] = createSignal<string | null>(null);

  // Add-entry form
  const [addOpen, setAddOpen] = createSignal(false);
  const [draft, setDraft] = createSignal({ ...EMPTY_DRAFT });
  const [addError, setAddError] = createSignal<string | null>(null);

  function resetPassphraseField() {
    setPassphrase("");
    setPassphraseError(null);
  }

  async function submitPassphrase() {
    const pw = passphrase();
    if (!pw) {
      setPassphraseError("Passphrase cannot be empty.");
      return;
    }
    setBusy(true);
    try {
      if (configured()) {
        await unlockVault(pw);
        toast.success("Vault unlocked");
      } else {
        await configureVault(pw);
        toast.success("Vault created");
      }
      resetPassphraseField();
    } catch (err) {
      setPassphraseError(
        errorText(
          err,
          configured() ? "Could not unlock the vault." : "Could not create it.",
        ).toUpperCase(),
      );
    } finally {
      setBusy(false);
    }
  }

  async function lock() {
    try {
      await lockVault();
      resetPassphraseField();
      toast.success("Vault locked");
    } catch (err) {
      toast.error(errorText(err, "Could not lock the vault."));
    }
  }

  function markCopied(key: string) {
    setCopiedId(key);
    timers.push(setTimeout(() => setCopiedId(null), 2000));
  }

  function copyPassword(entry: VaultEntry) {
    void navigator.clipboard.writeText(entry.password);
    markCopied(entry.id);
    toast.success("Password copied to clipboard");
  }

  function copyUsername(entry: VaultEntry) {
    void navigator.clipboard.writeText(entry.username);
    markCopied(`usr-${entry.id}`);
    toast.success("Username copied to clipboard");
  }

  async function submitEntry() {
    const input = draft();
    if (!input.name.trim()) {
      setAddError("Name is required.");
      return;
    }
    setBusy(true);
    try {
      await createVaultEntry({ ...input, name: input.name.trim() });
      setDraft({ ...EMPTY_DRAFT });
      setAddError(null);
      setAddOpen(false);
      toast.success(`SAVED "${input.name.trim()}"`);
    } catch (err) {
      setAddError(errorText(err, "Could not save the credential."));
    } finally {
      setBusy(false);
    }
  }

  async function handleDeleteEntry(entry: VaultEntry) {
    const ok = await confirm({
      title: `DELETE "${entry.name}"?`,
      detail:
        "This credential will be permanently removed from the vault. This action cannot be undone.",
      confirmLabel: "Delete",
      cancelLabel: "Cancel",
      tone: "alert",
    });
    if (!ok) return;
    try {
      await deleteVaultEntry(entry.id);
      toast.success(`DELETED "${entry.name}"`);
    } catch (err) {
      toast.error(errorText(err, "Could not delete the credential."));
    }
  }

  const view = createListView({
    source: () => entries() ?? [],
    search: (e) => `${e.name} ${e.url} ${e.username}`,
    sorts: {
      name: { label: "Name", compare: (a, b) => a.name.localeCompare(b.name) },
      url: { label: "URL", compare: (a, b) => a.url.localeCompare(b.url) },
    },
    initialSort: "name",
    initialDir: "asc",
  });

  return (
    <Stack gap={6}>
      <SettingsPageHeader
        section="vault"
        status={
          <StatusFlag status={locked() ? "alert" : "nominal"} dot>
            {locked() ? "Locked" : "Unlocked"}
          </StatusFlag>
        }
        actions={
          <Show when={!locked()}>
            <Button variant="danger" leading="lock" onClick={() => void lock()}>
              Lock
            </Button>
          </Show>
        }
      />

      <Suspense fallback={<LoadingText />}>
        {/* ── LOCKED / UNCONFIGURED STATE ────────────────────── */}
        <Show when={locked()}>
          <Panel
            label={configured() ? "Vault locked" : "Vault not set up"}
            state="alert"
          >
            <Stack gap={4}>
              <Row gap={3} align="center">
                <Icon name="lock" size={24} class="text-alert" />
                <Stack gap={1}>
                  <Row gap={2} align="center">
                    <Text variant="readout" tone="bright">
                      {configured()
                        ? "Vault passphrase required"
                        : "Choose a vault passphrase"}
                    </Text>
                    <InfoHint label="Credentials are sealed with a key derived from this passphrase, which is never stored and never leaves memory. It is separate from your login password: unlocking the app does not unlock the vault, and restarting re-locks it." />
                  </Row>
                  <Text variant="caption" tone="dim">
                    {configured()
                      ? "Enter the vault passphrase to access stored credentials."
                      : "No vault exists yet. The passphrase you choose here cannot be recovered."}
                  </Text>
                </Stack>
              </Row>
              <Input
                label={
                  configured() ? "Vault passphrase" : "New vault passphrase"
                }
                type="password"
                value={passphrase()}
                onInput={(e) => {
                  setPassphrase(e.currentTarget.value);
                  setPassphraseError(null);
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter") void submitPassphrase();
                }}
                invalid={passphraseError() !== null}
                hint={passphraseError() ?? undefined}
                placeholder="••••••••"
              />
              <Show when={!configured() && passphraseStrength(passphrase())}>
                {(s) => (
                  <Row gap={2} align="center">
                    <Text variant="micro" tone="dim">
                      Strength
                    </Text>
                    <StatusFlag status={s().status} dot>
                      {s().label}
                    </StatusFlag>
                    <Text variant="caption" tone="dim">
                      Use 12+ characters mixing case, digits, and symbols.
                    </Text>
                  </Row>
                )}
              </Show>
              <Button
                variant="primary"
                leading="key"
                disabled={busy()}
                onClick={() => void submitPassphrase()}
              >
                {configured() ? "Unlock vault" : "Create vault"}
              </Button>
              <Text variant="caption" tone="dim">
                Agent access to vault credentials pauses for your approval on
                every read.
              </Text>
            </Stack>
          </Panel>
        </Show>

        {/* ── UNLOCKED STATE ───────────────────────────────────── */}
        <Show when={!locked()}>
          <Panel label="Credentials" flush>
            <div class="p-3">
              <Row gap={3} align="center" justify="between">
                <ListToolbar
                  query={view.query()}
                  onQueryChange={view.setQuery}
                  placeholder="Search by name or URL…"
                  sortKey={view.sortKey()}
                  sortOptions={view.sortOptions}
                  onSortChange={view.setSort}
                  dir={view.dir()}
                  onToggleDir={view.toggleDir}
                  count={view.count()}
                  total={view.total()}
                />
                <Button
                  variant="primary"
                  size="sm"
                  leading="plus"
                  onClick={() => {
                    setDraft({ ...EMPTY_DRAFT });
                    setAddError(null);
                    setAddOpen(true);
                  }}
                >
                  Add
                </Button>
              </Row>
            </div>
            <For each={view.items()}>
              {(entry) => (
                <div>
                  <div class="flex items-start justify-between gap-3 px-3 py-3">
                    <Stack gap={1} class="min-w-0 flex-1">
                      <Row gap={2} align="center">
                        <Icon name="key" size={12} class="text-dim shrink-0" />
                        <Text variant="label" tone="bright" class="truncate">
                          {entry.name}
                        </Text>
                      </Row>
                      <Text variant="micro" tone="dim" class="truncate">
                        {entry.url}
                      </Text>
                      <Row gap={2} align="center" class="mt-1">
                        <Text variant="micro" tone="dim">
                          {entry.username}
                        </Text>
                        <Tooltip
                          label={
                            copiedId() === `usr-${entry.id}`
                              ? "Copied!"
                              : "Copy username"
                          }
                        >
                          <button
                            type="button"
                            class="text-dim transition-colors hover:text-bright"
                            onClick={() => copyUsername(entry)}
                          >
                            <Icon
                              name={
                                copiedId() === `usr-${entry.id}`
                                  ? "check"
                                  : "file"
                              }
                              size={10}
                            />
                          </button>
                        </Tooltip>
                      </Row>
                    </Stack>
                    <Row gap={2} align="center" class="shrink-0">
                      {/* Password field — copy-only; no plaintext render in DOM */}
                      <div class="flex items-center gap-2 bg-raised px-2 py-1">
                        <Text
                          variant="micro"
                          tone="dim"
                          class="font-mono w-36 truncate"
                        >
                          ••••••••••••
                        </Text>
                        <Tooltip
                          label={
                            copiedId() === entry.id
                              ? "Copied!"
                              : "Copy password"
                          }
                        >
                          <button
                            type="button"
                            class="text-dim transition-colors hover:text-bright"
                            onClick={() => copyPassword(entry)}
                          >
                            <Icon
                              name={copiedId() === entry.id ? "check" : "file"}
                              size={12}
                            />
                          </button>
                        </Tooltip>
                      </div>
                      <Menu
                        trigger={
                          <Button variant="ghost" size="sm" leading="menu" />
                        }
                        items={[
                          {
                            label: "Delete entry",
                            icon: "trash",
                            danger: true,
                            onSelect: () => void handleDeleteEntry(entry),
                          },
                        ]}
                        align="right"
                      />
                    </Row>
                  </div>
                </div>
              )}
            </For>
            <Show when={view.items().length === 0}>
              <EmptyState
                icon="key"
                message="No credentials"
                hint={
                  view.isFiltered()
                    ? "No entries match your search."
                    : "No vault entries yet — use ADD to store one."
                }
              />
            </Show>
          </Panel>
        </Show>
      </Suspense>

      {/* ── ADD ENTRY ────────────────────────────────────────── */}
      <Modal
        open={addOpen()}
        onClose={() => setAddOpen(false)}
        title="Add credential"
        footer={
          <>
            <Button variant="ghost" onClick={() => setAddOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="primary"
              disabled={busy()}
              onClick={() => void submitEntry()}
            >
              Save
            </Button>
          </>
        }
      >
        <Stack gap={3}>
          <Input
            label="Name"
            value={draft().name}
            onInput={(e) => {
              setDraft({ ...draft(), name: e.currentTarget.value });
              setAddError(null);
            }}
            invalid={addError() !== null}
            hint={addError() ?? undefined}
            placeholder="Production database"
          />
          <Input
            label="URL"
            value={draft().url}
            onInput={(e) =>
              setDraft({ ...draft(), url: e.currentTarget.value })
            }
            placeholder="https://…"
          />
          <Input
            label="Username"
            value={draft().username}
            onInput={(e) =>
              setDraft({ ...draft(), username: e.currentTarget.value })
            }
          />
          <Input
            label="Password"
            type="password"
            value={draft().password}
            onInput={(e) =>
              setDraft({ ...draft(), password: e.currentTarget.value })
            }
            placeholder="••••••••"
          />
        </Stack>
      </Modal>
    </Stack>
  );
}
