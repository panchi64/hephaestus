import { createSignal, Show, type JSX } from "solid-js";
import { Button, Drawer, Input, Row, Select, Stack, Text, toast } from "~/ui";
import type { McpAuthCredentials, McpServer } from "../model";

export interface McpAuthDrawerProps {
  /** The server being configured, or `null` when the drawer is closed. */
  server: McpServer | null;
  onClose: () => void;
  onSave: (serverId: string, creds: McpAuthCredentials) => void;
}

/** The credentials an auth-required server is dialled with. The values are
 *  write-only — the backend stores them encrypted and never reads them back — so
 *  the fields always start blank and saving replaces what is stored. */
export function McpAuthDrawer(props: McpAuthDrawerProps): JSX.Element {
  const [method, setMethod] =
    createSignal<McpAuthCredentials["method"]>("api_key");
  const [token, setToken] = createSignal("");
  const [username, setUsername] = createSignal("");
  const [password, setPassword] = createSignal("");

  const handleSave = () => {
    if (!props.server) return;

    const creds: McpAuthCredentials = { method: method() };
    if (method() === "api_key" || method() === "bearer") {
      if (!token().trim()) {
        toast.error("Token is required.");
        return;
      }
      creds.token = token().trim();
    } else {
      if (!username().trim() || !password().trim()) {
        toast.error("Username and password are required.");
        return;
      }
      creds.username = username().trim();
      creds.password = password().trim();
    }

    props.onSave(props.server.id, creds);
    props.onClose();
  };

  return (
    <Drawer
      open={props.server !== null}
      onClose={props.onClose}
      title="Configure auth"
      footer={
        <Row gap={2}>
          <Button variant="ghost" onClick={props.onClose}>
            Cancel
          </Button>
          <Button variant="primary" onClick={handleSave}>
            Save credentials
          </Button>
        </Row>
      }
    >
      <Show when={props.server}>
        {(srv) => (
          <Stack gap={4}>
            <Text variant="caption" tone="dim">
              Credentials are stored encrypted at rest and never read back —
              saving replaces what is stored. Server:{" "}
              <span class="text-bright">{srv().name}</span>
            </Text>

            <Select
              label="Auth method"
              value={method()}
              onChange={(v) => setMethod(v as McpAuthCredentials["method"])}
              options={[
                { value: "api_key", label: "API key" },
                { value: "bearer", label: "Bearer token" },
                { value: "basic", label: "Basic (user / pass)" },
              ]}
            />

            <Show when={method() === "api_key" || method() === "bearer"}>
              <Input
                label={method() === "api_key" ? "API key" : "Token"}
                type="password"
                value={token()}
                onInput={(e) => setToken(e.currentTarget.value)}
                placeholder="Paste your key here"
              />
            </Show>

            <Show when={method() === "basic"}>
              <Input
                label="Username"
                value={username()}
                onInput={(e) => setUsername(e.currentTarget.value)}
                placeholder="e.g. admin"
              />
              <Input
                label="Password"
                type="password"
                value={password()}
                onInput={(e) => setPassword(e.currentTarget.value)}
                placeholder="••••••••"
              />
            </Show>
          </Stack>
        )}
      </Show>
    </Drawer>
  );
}
