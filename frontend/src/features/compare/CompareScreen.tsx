import { Show, type JSX } from "solid-js";
import {
  ASSET_IDS,
  Button,
  Composer,
  PageHeader,
  Stack,
  Text,
  toast,
} from "~/ui";
import { createCompareController } from "./data";
import { ComparePaneView } from "./ComparePane";

/** Side-by-side model compare. One message is fanned to two independently-
 *  selected models and their answers stream in parallel. Each pane is a real
 *  (but ephemeral) chat conversation, so the comparison is a true multi-turn
 *  dialogue against each model — not a one-shot. All run lifecycle is
 *  backend-owned; this screen only relays intent.
 *
 *  It was the COMPARE tab of the Model Cookbook, and it is a page rather than a
 *  settings section for the reason it was never really a cookbook tab either: it
 *  is a bench you work at, two live transcripts wide, and it wants the room. */
export function CompareScreen(): JSX.Element {
  const compare = createCompareController();

  const stop = async () => {
    await compare.cancel();
    toast.success("Comparison stopped");
  };
  const reset = () => {
    compare.reset();
    toast.info("Comparison cleared");
  };

  return (
    <Stack gap={6}>
      <PageHeader
        title="Compare"
        subtitle="Run the same prompt across two models, side by side."
        assetId={ASSET_IDS.compare}
      />
      <div class="flex h-[70vh] min-h-[28rem] flex-col gap-3">
        <div class="flex items-center justify-between gap-3">
          <Text variant="caption" tone="dim">
            One message, sent to both models at once — pre-load both in your
            inference server (e.g. LM Studio) so neither stalls on a cold load.
            These threads are scratch and stay out of your chat history.
          </Text>
          <Show when={compare.started()}>
            <Button variant="ghost" size="sm" leading="refresh" onClick={reset}>
              New comparison
            </Button>
          </Show>
        </div>

        <div class="flex min-h-0 flex-1 flex-col gap-3 lg:flex-row">
          <ComparePaneView pane={compare.panes[0]} label="Model A" />
          <ComparePaneView pane={compare.panes[1]} label="Model B" />
        </div>

        <Composer
          disabled={compare.sending() || !compare.ready()}
          streaming={compare.sending()}
          onStop={() => void stop()}
          onSend={(text, ids) => compare.send(text, ids)}
          placeholder={
            compare.ready()
              ? "Message both models…"
              : "Select a model in each pane to begin…"
          }
          storageKey="compare:prompt"
        />
      </div>
    </Stack>
  );
}
