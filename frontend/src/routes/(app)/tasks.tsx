import { PluginGate } from "~/plugins/PluginGate";
import { TasksScreen } from "~/plugins/routines";

export default function TasksRoute() {
  return (
    <PluginGate plugin="routines" title="Routines">
      <TasksScreen />
    </PluginGate>
  );
}
