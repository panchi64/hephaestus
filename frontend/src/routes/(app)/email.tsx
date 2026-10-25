import { EmailInboxScreen } from "~/plugins/mail";
import { PluginGate } from "~/plugins/PluginGate";

export default function EmailRoute() {
  return (
    <PluginGate plugin="mail" title="Mail">
      <EmailInboxScreen />
    </PluginGate>
  );
}
