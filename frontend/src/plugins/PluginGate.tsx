import { Show, type JSX } from "solid-js";
import { useSettingsRoute } from "~/app/settings-dialog";
import { pluginEnabled } from "~/lib/stores/builtinPlugins";
import { Button, EmptyState } from "~/ui";

/** A built-in plugin's page, or a note that the plugin is switched off. */
export function PluginGate(props: {
  plugin: string;
  title: string;
  children: JSX.Element;
}): JSX.Element {
  const settings = useSettingsRoute();
  return (
    <Show
      when={pluginEnabled(props.plugin)}
      fallback={
        <EmptyState
          icon="plug"
          message={`${props.title} is switched off`}
          hint="Switch it on under Settings → Plugins."
          action={
            <Button size="sm" onClick={() => settings.show("plugins")}>
              Open plugins
            </Button>
          }
        />
      }
    >
      {props.children}
    </Show>
  );
}
