import { Show, type JSX } from "solid-js";
import { Combobox, Text, cx } from "~/ui";
import {
  effectiveValue,
  modelPickerGroups,
  modelsRefreshing,
  refreshEndpoints,
  refreshModels,
  selectModelByValue,
} from "~/lib/stores/models";

/** Which model the next turn runs on.
 *
 *  **This is the backend `main` role binding, not a per-surface preference.** The
 *  store writes `PUT /models/roles/main`, which is the same fact every server-initiated
 *  consumer resolves — titling, scheduled tasks. Changing it here changes it
 *  for all of them, deliberately: there is one operator and one active model, and a
 *  device-local override would be a second answer to a question the backend already
 *  owns. Every instance of this component is therefore a view of one value and they
 *  cannot disagree.
 *
 *  It lives **in the composer's status bar** rather than the top bar. The model is not
 *  a property of the application, it is a property of the message about to be sent, and
 *  in the top bar it was a persistent global control on every screen — including the
 *  many with nothing to send. Under the input it is in the operator's eye-line at the one
 *  moment the choice matters, and it costs nothing on the screens that don't have one.
 *
 *  A `cell` because that bar is a ruled line of machine values, not a toolbar: a filled
 *  control there is the heaviest thing on it for a choice made once in a while (§7).
 *
 *  **It gives way first on a narrow bar.** A model id can run to forty characters, and
 *  the bar it shares with the level and the thread's readouts wraps at half width — so
 *  the picker is capped and truncates rather than pushing its neighbours onto a line of
 *  their own. The full name is one click away in the list, where it is not truncated.
 *
 *  **The list refreshes itself when it opens**, rather than sitting behind a refresh
 *  button. Opening the menu is already the operator saying "show me what I can pick",
 *  and a model that appeared since the app loaded — a local engine they just started —
 *  is exactly the one they opened it to find. The old button asked them to say it
 *  twice, and to somehow know the list in front of them was stale.
 *
 *  **And it says so while it does.** That refresh is a per-endpoint provider probe
 *  budgeted around three seconds, and until it answered the panel was the previous
 *  list — or, on a workspace whose backend hasn't been asked yet, the flat assertion
 *  that there are no models and the operator should go add an endpoint. Both are
 *  claims the app is in no position to make mid-probe. The note sits beside the
 *  trigger rather than inside the panel because the panel belongs to `Combobox`,
 *  which is shared: a state this picker alone has would be a prop on every other
 *  dropdown in the app that has nothing to refresh. */
export function ModelPicker(props: { class?: string }): JSX.Element {
  return (
    <div
      class={cx("flex min-w-0 max-w-44 items-center sm:max-w-56", props.class)}
    >
      <Combobox
        cell
        // The popover root is `inline-flex`, which will not shrink below its content
        // unless told it may — without this the cap above would clip, not truncate.
        class="min-w-0"
        groups={modelPickerGroups()}
        value={effectiveValue()}
        onChange={selectModelByValue}
        onOpen={() => {
          // Both: the catalog may have gained an endpoint (added in Settings in
          // another tab), and the endpoints it already had may serve models they
          // didn't before. Neither implies the other, and only the second needs
          // asking the provider.
          refreshEndpoints();
          refreshModels();
        }}
        align="right"
        placeholder="No model"
        searchPlaceholder="Search models…"
        emptyHint="No models — add an endpoint in settings"
        aria-label="Active model"
      />
      <Show when={modelsRefreshing()}>
        <Text variant="micro" tone="dim" class="shrink-0 pr-3">
          Checking…
        </Text>
      </Show>
    </div>
  );
}
