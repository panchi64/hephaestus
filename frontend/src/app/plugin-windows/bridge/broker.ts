/** The host end of a plugin window's bridge.
 *
 *  On each load of the window's frame the host hands it a fresh `MessagePort`; every
 *  call afterwards arrives on that port, so nothing else on the page can speak for the
 *  window. A method the host answers itself (closing the window, starting a chat) is in
 *  `CLIENT_METHODS`; every other one goes to `POST /plugins/bridge/{method}` under the
 *  window's token, where the backend checks the plugin declared it and decides whether
 *  the operator has to confirm it first. */
import { api, isApiError } from "~/lib/api";
import { choose } from "~/ui";
import { subscribeEvents } from "./events";
import {
  PROTOCOL_VERSION,
  failure,
  parseRequest,
  type BridgeInit,
  type BridgeResponse,
} from "./protocol";

export interface BridgeHost {
  plugin: string;
  window: string;
  title: string;
  bridgeToken: string;
  permissions: readonly string[];
  close: () => void;
  openChat: (prompt: string | undefined) => void;
  /** The backend closed this window's session (the plugin changed or the app locked):
   *  open a new one, which reloads the page. */
  reopen: () => void;
}

type RemoteOutcome =
  | { outcome: "done"; result: unknown }
  | { outcome: "confirm"; confirmation_id: string; summary: string };

interface ClientMethod {
  /** The `permissions` entry the plugin must declare; none for its own window. */
  permission?: string;
  run: (params: Record<string, unknown>, host: BridgeHost) => unknown;
}

const CLIENT_METHODS: Readonly<Record<string, ClientMethod>> = {
  "window.close": { run: (_params, host) => host.close() },
  "chat.open": {
    permission: "chat.open",
    run: (params, host) =>
      host.openChat(
        typeof params.prompt === "string" ? params.prompt : undefined,
      ),
  },
};

/** The status the bridge answers once the window's session is gone. */
const SESSION_CLOSED = 410;

async function callLocal(
  method: ClientMethod,
  params: Record<string, unknown>,
  host: BridgeHost,
): Promise<unknown> {
  if (method.permission && !host.permissions.includes(method.permission))
    throw new Error(
      `this plugin does not declare the ${method.permission} permission`,
    );
  return method.run(params, host);
}

/** A window may keep this many calls open at once, and start this many per second. */
const MAX_IN_FLIGHT = 64;
const BURST = 30;
const PER_SECOND = 15;

function tokenBucket(): () => boolean {
  let tokens = BURST;
  let last = Date.now();
  return () => {
    const now = Date.now();
    tokens = Math.min(BURST, tokens + ((now - last) / 1000) * PER_SECOND);
    last = now;
    if (tokens < 1) return false;
    tokens -= 1;
    return true;
  };
}

async function callRemote(
  method: string,
  params: Record<string, unknown>,
  host: BridgeHost,
): Promise<unknown> {
  const headers = { "X-Plugin-Window": host.bridgeToken };
  let asked: RemoteOutcome;
  try {
    asked = await api.post<RemoteOutcome>(`/plugins/bridge/${method}`, params, {
      headers,
    });
  } catch (err) {
    if (isApiError(err) && err.status === SESSION_CLOSED) host.reopen();
    throw err;
  }
  if (asked.outcome === "done") return asked.result;
  const choice = await choose({
    title: `${host.title} wants to ${asked.summary}`,
    detail: "A plugin window is asking. Its code was written by an agent.",
    confirmLabel: "Allow once",
    alternateLabel: "Always allow",
    cancelLabel: "Deny",
  });
  if (choice === "cancel") throw new Error("the operator declined");
  const settled = await api.post<RemoteOutcome>(
    `/plugins/bridge/confirmations/${encodeURIComponent(asked.confirmation_id)}`,
    { always: choice === "alternate" },
    { headers },
  );
  if (settled.outcome !== "done") throw new Error("the call was not confirmed");
  return settled.result;
}

/** Connects a freshly loaded frame; returns what disconnects it. */
export function connectBridge(
  frame: HTMLIFrameElement,
  host: BridgeHost,
): () => void {
  const target = frame.contentWindow;
  if (!target) return () => undefined;
  const channel = new MessageChannel();
  const port = channel.port1;
  const allow = tokenBucket();
  let inFlight = 0;

  const reply = (response: BridgeResponse): void => port.postMessage(response);

  port.onmessage = (event: MessageEvent<unknown>) => {
    const request = parseRequest(event.data);
    if (typeof request === "string") {
      const raw = event.data as { id?: unknown } | null;
      reply(
        failure(
          typeof raw?.id === "number" ? raw.id : -1,
          "bad_request",
          request,
        ),
      );
      return;
    }
    if (!allow() || inFlight >= MAX_IN_FLIGHT) {
      reply(failure(request.id, "rate_limited", "too many calls; slow down"));
      return;
    }
    inFlight += 1;
    const run = Object.hasOwn(CLIENT_METHODS, request.method)
      ? callLocal(CLIENT_METHODS[request.method], request.params, host)
      : callRemote(request.method, request.params, host);
    run
      .then((result) =>
        reply({ id: request.id, ok: true, result: result ?? null }),
      )
      .catch((err: unknown) =>
        reply(
          failure(
            request.id,
            isApiError(err) ? `http_${err.status}` : "failed",
            isApiError(err)
              ? err.detail
              : err instanceof Error
                ? err.message
                : "the call failed",
          ),
        ),
      )
      .finally(() => {
        inFlight -= 1;
      });
  };

  const init: BridgeInit = {
    odysseus: "init",
    v: PROTOCOL_VERSION,
    plugin: host.plugin,
    window: host.window,
    permissions: [...host.permissions],
  };
  // The frame's origin is opaque, so "*" is the only target that reaches it; nothing
  // in the init message is secret, and the port it carries is this window's alone.
  target.postMessage(init, "*", [channel.port2]);
  const stopEvents = subscribeEvents(host.permissions, (message) =>
    port.postMessage(message),
  );
  return () => {
    stopEvents();
    port.close();
  };
}
