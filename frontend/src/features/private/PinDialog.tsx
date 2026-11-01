import { Match, Show, Switch, createSignal, type JSX } from "solid-js";
import { isApiError } from "~/lib/api";
import {
  ASSET_IDS,
  Button,
  FramedOverlay,
  Input,
  Reveal,
  Row,
  Stack,
  StatusFlag,
  Text,
} from "~/ui";
import { setPrivatePin, unlockPrivate } from "./actions";
import { pinRequest, privateStatus, settlePin, type PinMode } from "./store";

const TITLE_ID = "private-pin-title";
const PIN_PATTERN = /^\d{4,12}$/;

/** The one PIN prompt, mounted once at the app root and opened through `requestPin`.
 *
 *  The unlock plate's register (the login and setup plates of the design system's
 *  construction reveal) on the framed overlay,
 *  because it is the same act: a place opened with a secret. */
export function PinDialogHost(): JSX.Element {
  return (
    <FramedOverlay
      open={pinRequest() !== null}
      onClose={() => settlePin(false)}
      labelledBy={TITLE_ID}
      class="max-w-sm"
    >
      <Show when={pinRequest()} keyed>
        {(request) => <PinForm initial={request.mode} />}
      </Show>
    </FramedOverlay>
  );
}

/** `reset` is `change` proved with the account password instead of the old PIN. */
type FormMode = PinMode | "reset";

function PinForm(props: { initial: PinMode }): JSX.Element {
  const [mode, setMode] = createSignal<FormMode>(props.initial);
  const [pin, setPin] = createSignal("");
  const [confirmPin, setConfirmPin] = createSignal("");
  const [proof, setProof] = createSignal("");
  const [error, setError] = createSignal("");
  const [pending, setPending] = createSignal(false);

  const choosing = () => mode() !== "unlock";
  const minutes = () => privateStatus()?.relock_minutes ?? 15;

  const switchTo = (next: FormMode): void => {
    setMode(next);
    setPin("");
    setConfirmPin("");
    setProof("");
    setError("");
  };

  function problem(): string | null {
    if (!PIN_PATTERN.test(pin())) return "The PIN is 4 to 12 digits.";
    if (choosing() && pin() !== confirmPin()) return "The PINs don't match.";
    if ((mode() === "change" || mode() === "reset") && !proof())
      return mode() === "reset"
        ? "Enter your account password."
        : "Enter your current PIN.";
    return null;
  }

  async function submit(e: SubmitEvent): Promise<void> {
    e.preventDefault();
    const invalid = problem();
    if (invalid) {
      setError(invalid);
      return;
    }
    setError("");
    setPending(true);
    try {
      const current = mode();
      if (current === "unlock") await unlockPrivate(pin());
      else if (current === "set") await setPrivatePin(pin());
      else if (current === "change")
        await setPrivatePin(pin(), { currentPin: proof() });
      else await setPrivatePin(pin(), { password: proof() });
      settlePin(true);
    } catch (err) {
      setError(describe(err, mode()));
    } finally {
      setPending(false);
    }
  }

  return (
    <form onSubmit={(e) => void submit(e)} class="flex flex-col gap-4 p-6">
      <Stack gap={1}>
        <div id={TITLE_ID}>
          <Switch>
            <Match when={mode() === "unlock"}>
              <StatusFlag status="idle" dot>
                Private chats locked
              </StatusFlag>
            </Match>
            <Match when={mode() === "set"}>
              <StatusFlag status="idle" dot>
                Choose a PIN
              </StatusFlag>
            </Match>
            <Match when={mode() === "change" || mode() === "reset"}>
              <StatusFlag status="idle" dot>
                Change PIN
              </StatusFlag>
            </Match>
          </Switch>
        </div>
        <Text variant="caption" tone="dim">
          <Switch>
            <Match when={mode() === "unlock"}>
              Enter your PIN to open private chats. They lock again after{" "}
              {minutes()} idle minutes.
            </Match>
            <Match when={mode() === "set"}>
              Private chats are hidden from other sessions, and every read of
              one asks you first. Choose a 4 to 12 digit PIN to open them.
            </Match>
            <Match when={mode() === "change"}>
              Enter your current PIN, then the new one.
            </Match>
            <Match when={mode() === "reset"}>
              Enter your account password, then a new PIN.
            </Match>
          </Switch>
        </Text>
      </Stack>

      <Show when={mode() === "change" || mode() === "reset"}>
        <Input
          label={mode() === "reset" ? "Password" : "Current PIN"}
          type="password"
          inputMode={mode() === "reset" ? "text" : "numeric"}
          autocomplete={mode() === "reset" ? "current-password" : "off"}
          value={proof()}
          onInput={(e) => {
            setProof(e.currentTarget.value);
            setError("");
          }}
        />
      </Show>
      <Input
        label={choosing() ? "New PIN" : "PIN"}
        type="password"
        inputMode="numeric"
        autocomplete="off"
        autofocus
        maxLength={12}
        placeholder="••••"
        value={pin()}
        onInput={(e) => {
          setPin(e.currentTarget.value);
          setError("");
        }}
      />
      <Show when={choosing()}>
        <Input
          label="Confirm PIN"
          type="password"
          inputMode="numeric"
          autocomplete="off"
          maxLength={12}
          value={confirmPin()}
          onInput={(e) => {
            setConfirmPin(e.currentTarget.value);
            setError("");
          }}
        />
      </Show>
      <Show when={error()}>
        <Reveal>
          <Text variant="caption" tone="alert">
            {error()}
          </Text>
        </Reveal>
      </Show>

      <Row gap={2} justify="end">
        <Show when={mode() === "unlock" || mode() === "change"}>
          <Button
            variant="ghost"
            type="button"
            class="mr-auto"
            onClick={() => switchTo("reset")}
          >
            Forgot PIN
          </Button>
        </Show>
        <Button variant="ghost" type="button" onClick={() => settlePin(false)}>
          Cancel
        </Button>
        <Button
          variant="primary"
          type="submit"
          pending={pending()}
          pendingLabel={mode() === "unlock" ? "Unlocking" : "Saving"}
        >
          {mode() === "unlock" ? "Unlock" : "Save PIN"}
        </Button>
      </Row>
      <Text variant="plate" tone="dim" class="self-center">
        {ASSET_IDS.privateArea}
      </Text>
    </form>
  );
}

function describe(err: unknown, mode: FormMode): string {
  if (!isApiError(err)) return "Unable to reach the backend.";
  if (err.status === 429) return "Too many attempts. Wait a moment.";
  if (err.status === 403)
    return mode === "unlock"
      ? "Incorrect PIN. Five wrong in a row signs you out."
      : mode === "reset"
        ? "Incorrect password."
        : "Incorrect current PIN.";
  return err.detail;
}
