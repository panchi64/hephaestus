import { createSignal, Show, type JSX } from "solid-js";
import { Modal } from "./Modal";
import { Button } from "./Button";
import { Input } from "./Input";
import { Stack } from "../primitives/Stack";
import { Text } from "../primitives/Text";

/** Promise-based confirmation gate for destructive / consequential actions.
 *  Built on Modal + Button so every guard looks and behaves the same. */
export type ConfirmTone = "alert" | "default";

export interface ConfirmOptions {
  title: string;
  /** Body line; defaults to "This action cannot be undone." */
  detail?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  /** alert => danger confirm button (destructive). default => primary. */
  tone?: ConfirmTone;
  /** When set, the confirm button stays disabled until the operator types this
   *  word exactly. For the small class of actions where a mis-click is
   *  unrecoverable — not a general "are you sure" upgrade, which would only
   *  train the reflex it exists to interrupt. */
  requireText?: string;
  /** Rendered under the detail line, for a body the plain sentence cannot carry. */
  content?: () => JSX.Element;
}

export type ConfirmChoice = "confirm" | "alternate" | "cancel";

/** A dialog with a third way out, between confirming and cancelling. */
export interface ChooseOptions extends ConfirmOptions {
  /** The middle button: go on without what the confirm button does. */
  alternateLabel: string;
}

interface ConfirmState extends ChooseOptions {
  resolve: (choice: ConfirmChoice) => void;
}

const [current, setCurrent] = createSignal<ConfirmState | null>(null);

function open(
  opts: ConfirmOptions & { alternateLabel?: string },
): Promise<ConfirmChoice> {
  return new Promise((resolve) => {
    setCurrent((prev) => {
      prev?.resolve("cancel"); // supersede any open dialog as cancelled
      return { alternateLabel: "", ...opts, resolve };
    });
  });
}

/** Await a user's confirmation before a dangerous action:
 *  `if (await confirm({ title: "Delete report?", tone: "alert" })) remove();`
 *  Cancel, the X, the backdrop, and Escape all resolve `false`.
 *  Render <ConfirmHost/> once at the app root. */
export async function confirm(opts: ConfirmOptions): Promise<boolean> {
  return (await open(opts)) === "confirm";
}

/** Like `confirm`, with a middle choice. Cancel, the X, the backdrop and Escape resolve
 *  `"cancel"`. */
export function choose(opts: ChooseOptions): Promise<ConfirmChoice> {
  return open(opts);
}

function settle(choice: ConfirmChoice): void {
  const c = current();
  if (!c) return;
  setCurrent(null);
  setTyped("");
  c.resolve(choice);
}

/** The gate word as typed so far. Held beside `current` rather than inside it so
 *  opening a dialog can't inherit the previous one's progress. */
const [typed, setTyped] = createSignal("");

const gated = (): boolean => {
  const want = current()?.requireText;
  return want !== undefined && typed() !== want;
};

/** The single confirmation dialog outlet. Mount once at the root. */
export function ConfirmHost(): JSX.Element {
  return (
    <Modal
      open={current() !== null}
      onClose={() => settle("cancel")}
      title={current()?.title}
      footer={
        <>
          <Button variant="ghost" onClick={() => settle("cancel")}>
            {current()?.cancelLabel ?? "Cancel"}
          </Button>
          <Show when={current()?.alternateLabel}>
            {(label) => (
              <Button variant="default" onClick={() => settle("alternate")}>
                {label()}
              </Button>
            )}
          </Show>
          <Button
            variant={current()?.tone === "alert" ? "danger" : "primary"}
            disabled={gated()}
            onClick={() => settle("confirm")}
          >
            {current()?.confirmLabel ?? "Confirm"}
          </Button>
        </>
      }
    >
      <Stack gap={3}>
        <Text tone="dim">
          {current()?.detail ?? "This action cannot be undone."}
        </Text>
        {current()?.content?.()}
        <Show when={current()?.requireText}>
          {(word) => (
            <Input
              label={`Type ${word()} to confirm`}
              value={typed()}
              onInput={(e) => setTyped(e.currentTarget.value)}
              autocomplete="off"
              spellcheck={false}
            />
          )}
        </Show>
      </Stack>
    </Modal>
  );
}
