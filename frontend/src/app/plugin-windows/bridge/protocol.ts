/** The messages a plugin window and the host exchange over the window's message port.
 *
 *  The window's frame runs in an opaque origin, so nothing it says is trusted: every
 *  request is validated here before the broker looks at it, and the backend checks
 *  again that this window's plugin declared the permission it calls under. */

export const PROTOCOL_VERSION = 1;
/** The largest request a window may send, serialized. */
export const MAX_MESSAGE_BYTES = 256 * 1024;

export interface BridgeRequest {
  id: number;
  method: string;
  params: Record<string, unknown>;
}

export type BridgeResponse =
  | { id: number; ok: true; result: unknown }
  | { id: number; ok: false; error: { code: string; message: string } };

export interface BridgeEvent {
  event: string;
  data: unknown;
}

export interface BridgeInit {
  odysseus: "init";
  v: number;
  plugin: string;
  window: string;
  permissions: string[];
}

const METHOD = /^[a-z][a-z_]*(\.[a-z][a-z_]*)+$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The request in ``raw``, or a reason it isn't one. */
export function parseRequest(raw: unknown): BridgeRequest | string {
  if (!isRecord(raw)) return "a request is an object";
  const { id, method, params } = raw;
  if (typeof id !== "number" || !Number.isInteger(id))
    return "id must be an integer";
  if (typeof method !== "string" || !METHOD.test(method))
    return "method must look like `area.verb`";
  if (params !== undefined && !isRecord(params))
    return "params must be an object";
  let size: number;
  try {
    size = JSON.stringify(raw).length;
  } catch {
    return "a request must be plain JSON";
  }
  if (size > MAX_MESSAGE_BYTES) return "the request is too large";
  return { id, method, params: params ?? {} };
}

export function failure(
  id: number,
  code: string,
  message: string,
): BridgeResponse {
  return { id, ok: false, error: { code, message } };
}
