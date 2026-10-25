import { createSignal, Show, type JSX } from "solid-js";
import { isApiError } from "~/lib/api";
import { useSession } from "~/lib/stores/session";
import { Button, Input, Reveal, Stack, StatusFlag, Text } from "~/ui";

/**
 * First-run setup: choose the operator password. This derives the vault key and
 * unlocks the workspace — there is exactly one operator, so this runs once. The gate
 * then shows the first-run walkthrough.
 */
export function SignupScreen(): JSX.Element {
  const session = useSession();

  const [password, setPassword] = createSignal("");
  const [confirm, setConfirm] = createSignal("");
  const [error, setError] = createSignal("");
  const [loading, setLoading] = createSignal(false);

  const mismatch = () => confirm().length > 0 && password() !== confirm();

  async function handleCreate(e: SubmitEvent) {
    e.preventDefault();
    if (password().length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }
    if (password() !== confirm()) {
      setError("Passwords do not match.");
      return;
    }
    setError("");
    setLoading(true);
    try {
      // Setup flips the session to "unlocked" and the auth gate swaps this screen
      // for the app in the same tick. There is no navigation to do — which is the
      // point: routing here is what used to leave the operator staring at the form.
      await session.setup(password());
    } catch (err) {
      setError(
        isApiError(err) && err.status === 409
          ? "The workspace is already initialized."
          : "Unable to complete setup. Check the backend connection.",
      );
    } finally {
      setLoading(false);
    }
  }

  return (
    <form onSubmit={handleCreate}>
      <Stack gap={3}>
        <Stack gap={1}>
          <StatusFlag status="info" dot>
            First-run setup
          </StatusFlag>
          <Text variant="micro" tone="dim">
            Choose the operator password. It derives the encryption key for all
            stored data and is never recoverable — store it safely.
          </Text>
        </Stack>
        <Input
          label="Password"
          type="password"
          value={password()}
          onInput={(e) => {
            setPassword(e.currentTarget.value);
            setError("");
          }}
          placeholder="••••••••"
          hint="Minimum 8 characters."
          autocomplete="new-password"
        />
        <Input
          label="Confirm password"
          type="password"
          value={confirm()}
          onInput={(e) => {
            setConfirm(e.currentTarget.value);
            setError("");
          }}
          placeholder="••••••••"
          invalid={mismatch()}
          hint={mismatch() ? "Passwords do not match." : undefined}
          autocomplete="new-password"
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
          disabled={mismatch()}
          pending={loading()}
          pendingLabel="Initializing"
        >
          Initialize workspace
        </Button>
      </Stack>
    </form>
  );
}
