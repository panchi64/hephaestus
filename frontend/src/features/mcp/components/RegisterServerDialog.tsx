import { createSignal, Show, type JSX } from "solid-js";
import { Button, Input, Modal, Row, Select, Stack, Text, toast } from "~/ui";
import { mcpErrorMessage, registerMcpServer } from "../data";
import type { McpTransport } from "../model";

export interface RegisterServerDialogProps {
  open: boolean;
  onClose: () => void;
}

/** Register a server and dial it in one step.
 *
 *  Owns the field values and the register call; the caller only says whether it
 *  is open. Registration connects, so the response already carries the outcome —
 *  a server that refuses comes back registered-but-erroring rather than throwing,
 *  and the toast says which happened. */
export function RegisterServerDialog(
  props: RegisterServerDialogProps,
): JSX.Element {
  const [name, setName] = createSignal("");
  const [transport, setTransport] = createSignal<McpTransport>("stdio");
  const [url, setUrl] = createSignal("");
  const [command, setCommand] = createSignal("");
  const [registering, setRegistering] = createSignal(false);

  /** The command line is typed as one string and split into argv here — a
   *  presentation convenience only; the backend stores command and args apart. */
  const commandParts = (): { command: string; args: string[] } => {
    const [head = "", ...args] = command().trim().split(/\s+/);
    return { command: head, args };
  };

  const canRegister = () =>
    name().trim() !== "" &&
    (transport() === "stdio" ? command().trim() : url().trim()) !== "";

  const reset = () => {
    setName("");
    setUrl("");
    setCommand("");
    setTransport("stdio");
  };

  const submit = async () => {
    if (!canRegister()) return;
    const label = name().trim();
    const stdio = transport() === "stdio";
    setRegistering(true);
    try {
      const created = await registerMcpServer({
        name: label,
        transport: transport(),
        ...(stdio ? commandParts() : { url: url().trim() }),
      });
      reset();
      props.onClose();
      if (created.status === "connected") {
        toast.success(
          `"${label}" registered — ${created.tools.length} tools discovered.`,
        );
      } else {
        toast.error(
          created.errorMessage ??
            `"${label}" registered but did not connect. Use RETRY once it is running.`,
        );
      }
    } catch (err) {
      toast.error(mcpErrorMessage(err, `Could not register "${label}".`));
    } finally {
      setRegistering(false);
    }
  };

  return (
    <Modal
      open={props.open}
      onClose={props.onClose}
      title="Register MCP server"
      footer={
        <Row gap={2}>
          <Button variant="ghost" onClick={props.onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            onClick={() => void submit()}
            disabled={!canRegister() || registering()}
          >
            {registering() ? "Connecting…" : "Register"}
          </Button>
        </Row>
      }
    >
      <Stack gap={4}>
        <Input
          label="Server name"
          value={name()}
          onInput={(e) => setName(e.currentTarget.value)}
          placeholder="e.g. My Custom MCP"
        />
        <Select
          label="Transport"
          value={transport()}
          onChange={(v) => setTransport(v as McpTransport)}
          options={[
            { value: "stdio", label: "Stdio (local subprocess)" },
            { value: "http", label: "HTTP (streamable)" },
            { value: "sse", label: "SSE" },
          ]}
        />
        <Show
          when={transport() === "stdio"}
          fallback={
            <Input
              label="URL"
              value={url()}
              onInput={(e) => setUrl(e.currentTarget.value)}
              placeholder="e.g. http://localhost:8080/mcp"
            />
          }
        >
          <Input
            label="Command"
            value={command()}
            onInput={(e) => setCommand(e.currentTarget.value)}
            placeholder="e.g. npx -y @modelcontextprotocol/server-name"
          />
        </Show>
        <Text variant="caption" tone="dim">
          Registering connects to the server and discovers its tools. Each tool
          arrives switched on but untrusted, so it will ask before it runs. For
          auth-required servers, use CONFIGURE AUTH on the server card.
        </Text>
      </Stack>
    </Modal>
  );
}
