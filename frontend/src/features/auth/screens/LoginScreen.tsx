import { createSignal, Show, type JSX } from "solid-js";
import { isApiError } from "~/lib/api";
import { useSession } from "~/lib/stores/session";
import { bytes } from "~/lib/format";
import {
  Button,
  confirm,
  Input,
  Reveal,
  Stack,
  StatusFlag,
  Text,
  toast,
} from "~/ui";

/**
 * Unlock the workspace. The vault key is password-derived and memory-only, so
 * "sign in" is really "unlock" — one password, no username, no 2FA.
 *
 * Rendered by the auth gate (`~/lib/guards`), not by a route: unlocking flips the
 * session signal and the gate swaps this screen for the app in the same tick.
 */
export function LoginScreen(): JSX.Element {
  const session = useSession();

  const [password, setPassword] = createSignal("");
  const [error, setError] = createSignal("");
  const [loading, setLoading] = createSignal(false);
  const [resetting, setResetting] = createSignal(false);

  async function handleUnlock(e: SubmitEvent) {
    e.preventDefault();
    if (!password().trim()) {
      setError("Password is required.");
      return;
    }
    setError("");
    setLoading(true);
    try {
      await session.unlock(password());
    } catch (err) {
      setError(
        isApiError(err) && err.status === 401
          ? "Incorrect password."
          : "Unable to unlock. Check the backend connection.",
      );
    } finally {
      setLoading(false);
    }
  }

  /** Start over on a workspace whose database is gone. Everything the key protects
   *  goes with it, so the confirmation names it rather than saying "cannot be
   *  undone" and leaving the operator to guess what "it" covers. */
  async function handleReset() {
    const ok = await confirm({
      title: "Start fresh?",
      tone: "alert",
      confirmLabel: "Delete and start over",
      requireText: "RESET",
      detail:
        "This permanently deletes the encryption key and everything stored on top of the database, under data/ — uploaded files and their extracted text, the knowledge base and its index, sandbox workspaces, saved View snapshots, and the browser profile. All of it is sealed under the key being removed, so none of it is readable afterwards, by this app or anything else. Your project directories and worktrees live outside data/ and are not touched. This cannot be undone.",
    });
    if (!ok) return;
    setError("");
    setResetting(true);
    try {
      const summary = await session.resetWorkspace();
      toast.success(
        `Workspace cleared — removed ${summary.removed.length} item(s), ${bytes(summary.bytesFreed)} freed.`,
      );
      if (summary.failed.length > 0) {
        toast.error(
          `Could not remove: ${summary.failed.join(", ")}. Delete them by hand.`,
        );
      }
      // Nothing to navigate: the status flips to `uninitialized` and the gate
      // shows setup.
    } catch (err) {
      setError(
        isApiError(err)
          ? `Could not start fresh: ${err.detail}`
          : "Could not start fresh. Check the backend connection.",
      );
    } finally {
      setResetting(false);
    }
  }

  return (
    <form onSubmit={handleUnlock}>
      <Stack gap={3}>
        <Stack gap={1}>
          <StatusFlag status="idle" dot>
            Workspace locked
          </StatusFlag>
          <Text variant="micro" tone="dim">
            Enter the operator password to unlock encrypted storage.
          </Text>
        </Stack>

        {/* The key outlived its database. Both readings of that are legitimate —
            carry on in an empty workspace, or start over — so both are offered
            rather than one being chosen on the operator's behalf. */}
        <Show when={session.dbMissing}>
          <Stack gap={1}>
            <StatusFlag status="warn" dot>
              Database missing
            </StatusFlag>
            <Text variant="micro" tone="dim">
              The workspace database is gone, but its encryption key is still
              here. Unlock with your existing password to carry on in an empty
              workspace, or start fresh to delete the key and everything sealed
              under it.
            </Text>
          </Stack>
        </Show>

        <Show when={session.probeError}>
          {(detail) => (
            <Text variant="micro" tone="alert">
              The backend answered with an error: {detail()}
            </Text>
          )}
        </Show>

        <Input
          label="Password"
          type="password"
          value={password()}
          onInput={(e) => {
            setPassword(e.currentTarget.value);
            setError("");
          }}
          placeholder="••••••••"
          autocomplete="current-password"
        />
        <Show when={error()}>
          <Reveal>
            <Text variant="micro" tone="alert">
              {error()}
            </Text>
          </Reveal>
        </Show>
        <Button
          variant="primary"
          type="submit"
          pending={loading()}
          pendingLabel="Unlocking"
        >
          Unlock
        </Button>
        <Show when={session.dbMissing}>
          <Button
            variant="danger"
            type="button"
            pending={resetting()}
            pendingLabel="Clearing"
            onClick={handleReset}
          >
            Start fresh
          </Button>
        </Show>
      </Stack>
    </form>
  );
}
