import {
  createEffect,
  createMemo,
  onCleanup,
  onMount,
  Show,
  type JSX,
} from "solid-js";
import { Button, Frames, Row, Stack, StatusFlag, Text, toast } from "~/ui";
import { isApiError } from "~/lib/api";
import {
  refreshCapabilityHealth,
  refreshReindexStatus,
  triggerReindex,
  useEmbeddingHealth,
  useReindexStatus,
} from "../data";

/** The search & memory model's live readouts: the backend's recall-health verdict
 *  and the re-embed (reindex) the workspace runs after a model change. Which model
 *  serves embeddings is picked on the card that hosts this. Pure presentation —
 *  every decision (whether recall is degraded, when a reindex is needed) is the
 *  backend's; this only renders that state and relays the operator's intent. */
export interface EmbeddingRoleControlsProps {
  /** Whether any endpoint is bound to the embedding role. */
  bound: boolean;
}

export function EmbeddingRoleControls(
  props: EmbeddingRoleControlsProps,
): JSX.Element {
  const health = useEmbeddingHealth();
  const reindex = useReindexStatus();

  // Seed both readouts once on mount (signals start empty — no Suspense).
  onMount(() => {
    void refreshReindexStatus();
    void refreshCapabilityHealth();
  });

  // Only re-subscribe when running actually toggles (a memo), not on every poll
  // payload — so the interval lives for the whole run instead of churning each tick.
  const running = createMemo(() => reindex()?.state === "running");
  createEffect(() => {
    if (!running()) return;
    const timer = setInterval(() => void refreshReindexStatus(), 1500);
    onCleanup(() => clearInterval(timer));
  });

  const reembed = async () => {
    try {
      await triggerReindex();
      toast.success("Re-embedding started.");
    } catch (e) {
      toast.error(isApiError(e) ? e.detail : "Unable to start re-embedding.");
    }
  };

  const statusLine = createMemo<string>(() => {
    const r = reindex();
    if (r?.state === "running") return "Re-indexing…";
    if (r?.state === "done")
      return `RE-EMBEDDED ${r.memories} MEMORIES · ${r.messages} MESSAGES`;
    if (r?.state === "degraded")
      return (r.detail ?? "no embedding model configured").toUpperCase();
    if (r?.state === "error") return "Re-embed failed";
    return "Re-embed after changing the embedding model.";
  });

  return (
    <Stack gap={2}>
      {/* The backend owns the verdict; we only render it. */}
      <Show when={health()?.status === "warn"}>
        <StatusFlag status="warn">
          {(health()?.detail ?? "keyword-only recall").toUpperCase()}
        </StatusFlag>
      </Show>

      <Row align="center" justify="between" gap={2}>
        <Row align="center" gap={2}>
          {/* The throbber makes the background re-embed legible as live work —
              so the operator can tie an unexpected GPU spike to this action. */}
          <Show when={running()}>
            <Frames class="text-warn" />
          </Show>
          <Text variant="micro" tone={running() ? "warn" : "dim"}>
            {statusLine()}
          </Text>
        </Row>
        <Button
          variant="ghost"
          size="sm"
          leading="refresh"
          disabled={running() || !props.bound}
          onClick={() => void reembed()}
        >
          Re-embed
        </Button>
      </Row>
    </Stack>
  );
}
