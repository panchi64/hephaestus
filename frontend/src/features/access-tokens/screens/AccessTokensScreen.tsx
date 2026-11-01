import { createSignal, For, Show, type JSX } from "solid-js";
import { settled } from "~/lib/resource";
import { date, relativeTime } from "~/lib/format";
import {
  Button,
  Checkbox,
  confirm,
  copyToClipboard,
  EmptyState,
  Input,
  LoadingText,
  Modal,
  Panel,
  Row,
  Stack,
  StatusFlag,
  Text,
  toast,
} from "~/ui";
import { SettingsPageHeader } from "~/app/settings-dialog/SettingsPageHeader";
import {
  issueAccessToken,
  revokeAccessToken,
  useAccessTokens,
  useTokenScopes,
} from "../data";
import type { AccessToken, IssuedAccessToken } from "../model";

/** Access Tokens — the credentials the operator issues so a *client* can call this API
 *  programmatically (`AUTH-4`). The other direction from the Service Keys section
 *  above it in SECURITY, which stores the keys this system calls third parties with.
 *
 *  The plaintext is returned once and never again, so issuing ends in a reveal the
 *  operator must copy out of — the one place in the app where closing a dialog loses
 *  something unrecoverable, and it says so. */
export function AccessTokensScreen(): JSX.Element {
  const tokens = useAccessTokens();
  const scopes = useTokenScopes();

  const [issuing, setIssuing] = createSignal(false);
  const [label, setLabel] = createSignal("");
  const [chosen, setChosen] = createSignal<string[]>([]);
  const [saving, setSaving] = createSignal(false);
  const [revealed, setRevealed] = createSignal<IssuedAccessToken | null>(null);

  const isChosen = (id: string) => chosen().includes(id);
  const toggleScope = (id: string) =>
    setChosen((ids) =>
      ids.includes(id) ? ids.filter((s) => s !== id) : [...ids, id],
    );

  const openIssue = () => {
    setLabel("");
    setChosen([]);
    setIssuing(true);
  };

  const submit = async () => {
    if (!label().trim() || chosen().length === 0 || saving()) return;
    setSaving(true);
    try {
      const issued = await issueAccessToken(label().trim(), chosen());
      setIssuing(false);
      setRevealed(issued);
    } catch {
      toast.error("Unable to issue the token.");
    }
    setSaving(false);
  };

  const copy = () => {
    const issued = revealed();
    if (issued) copyToClipboard(issued.token, "Token");
  };

  const revoke = async (token: AccessToken) => {
    if (
      !(await confirm({
        title: `Revoke "${token.label}"?`,
        detail:
          "Any client still presenting this token is refused from its next request. Revoking cannot be undone — issue a new token instead.",
        confirmLabel: "Revoke",
        tone: "alert",
      }))
    )
      return;
    try {
      await revokeAccessToken(token.id);
      toast.success(`${token.label} revoked`);
    } catch {
      toast.error("Unable to revoke the token.");
    }
  };

  /** The scope's declared label, falling back to its id if the catalog is still loading. */
  const scopeLabel = (id: string) =>
    settled(scopes)?.find((scope) => scope.id === id)?.label ?? id;

  return (
    <Stack gap={6}>
      <SettingsPageHeader
        section="access-tokens"
        actions={
          <Button variant="primary" size="sm" leading="key" onClick={openIssue}>
            Issue token
          </Button>
        }
      />

      <Panel label="Issued tokens" flush>
        <Show
          when={settled(tokens)}
          fallback={
            <div class="p-3">
              <LoadingText />
            </div>
          }
        >
          <Show
            when={(settled(tokens) ?? []).length > 0}
            fallback={
              <EmptyState
                message="No tokens issued"
                hint="Issue one to let a script, CLI, or automation reach this API without your password."
                icon="key"
              />
            }
          >
            <For each={settled(tokens) ?? []}>
              {(token) => (
                <Row align="center" justify="between" gap={3} class="px-3 py-3">
                  <Stack gap={1} class="min-w-0">
                    <Row gap={2} align="center">
                      <Text variant="label" tone="bright">
                        {token.label}
                      </Text>
                      <Show
                        when={token.revokedAt}
                        fallback={
                          <StatusFlag status="nominal">Active</StatusFlag>
                        }
                      >
                        <StatusFlag status="alert">Revoked</StatusFlag>
                      </Show>
                    </Row>
                    <Text variant="micro" tone="dim">
                      {token.prefix} · {token.scopes.map(scopeLabel).join(", ")}
                    </Text>
                    <Text variant="micro" tone="dim">
                      ISSUED {date(token.createdAt)} ·{" "}
                      {token.lastUsedAt
                        ? `LAST USED ${relativeTime(token.lastUsedAt)}`
                        : "Never used"}
                    </Text>
                  </Stack>
                  <Show when={!token.revokedAt}>
                    <Button
                      variant="ghost"
                      size="sm"
                      leading="trash"
                      class="shrink-0"
                      onClick={() => revoke(token)}
                    >
                      Revoke
                    </Button>
                  </Show>
                </Row>
              )}
            </For>
          </Show>
        </Show>
      </Panel>

      <Modal
        open={issuing()}
        onClose={() => setIssuing(false)}
        title="Issue access token"
        class="max-w-lg"
      >
        <Stack gap={3}>
          <Input
            label="Label"
            value={label()}
            onInput={(e) => setLabel(e.currentTarget.value)}
            placeholder="laptop CLI"
            hint="How you'll recognize this token later — the token itself is never shown again."
          />
          <Stack gap={2}>
            <Text variant="label" tone="bright">
              Scopes
            </Text>
            <Text variant="caption" tone="dim">
              The token reaches only what you grant here. Anything outside its
              scopes is refused — including issuing tokens, the vault, backups,
              and the host shell, which no token can ever reach.
            </Text>
            <Show when={settled(scopes)} fallback={<LoadingText />}>
              <For each={settled(scopes) ?? []}>
                {(scope) => (
                  <Stack gap={0}>
                    <Checkbox
                      label={scope.label}
                      checked={isChosen(scope.id)}
                      onChange={() => toggleScope(scope.id)}
                    />
                    <Text variant="caption" tone="dim" class="pl-6">
                      {scope.description}
                    </Text>
                  </Stack>
                )}
              </For>
            </Show>
          </Stack>
          <div class="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setIssuing(false)}>
              Cancel
            </Button>
            <Button
              variant="primary"
              disabled={!label().trim() || chosen().length === 0 || saving()}
              onClick={submit}
            >
              {saving() ? "Issuing…" : "Issue"}
            </Button>
          </div>
        </Stack>
      </Modal>

      <Modal
        open={revealed() !== null}
        onClose={() => setRevealed(null)}
        title="Copy your token now"
        class="max-w-lg"
      >
        <Stack gap={3}>
          <Text variant="caption" tone="warn">
            This is the only time the token is shown. Close this dialog and it
            is gone — the server keeps only a one-way hash. If you lose it,
            issue a new one.
          </Text>
          <Text
            variant="micro"
            tone="bright"
            class="bg-raised p-3 break-all select-all"
          >
            {revealed()?.token}
          </Text>
          <Text variant="caption" tone="dim">
            Send it as an Authorization header: Bearer &lt;token&gt;
          </Text>
          <div class="flex justify-end gap-2">
            <Button variant="ghost" leading="copy" onClick={copy}>
              Copy
            </Button>
            <Button variant="primary" onClick={() => setRevealed(null)}>
              Done
            </Button>
          </div>
        </Stack>
      </Modal>
    </Stack>
  );
}
