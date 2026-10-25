import { formatTokens, type LocalModel, type LocalServer } from "./model";

/** Talos's briefing for each step, written as Markdown so model names render in the
 *  machine voice. Two short parts: what the section is for, which never changes, then
 *  what Talos found or recommends, rewritten as the state changes. The fixed first part
 *  is what keeps a rewrite from re-streaming the whole briefing. */

const join = (...parts: (string | false | null | undefined)[]): string =>
  parts.filter(Boolean).join("\n\n");

const count = (n: number, noun: string): string =>
  `${n} ${n === 1 ? noun : `${noun}s`}`;

export function loadedModel(server: LocalServer): LocalModel | undefined {
  return server.models.find(
    (m) => m.loaded === true && m.kind !== "embeddings",
  );
}

export interface EndpointBriefing {
  scanning: boolean;
  /** Local servers no endpoint points at yet. */
  available: LocalServer[];
  /** The endpoint just added or edited, with its test verdict. */
  latest?: {
    name: string;
    ok: boolean;
    models: number;
    detail: string | null;
  };
  /** The provider form is open, for this provider. */
  form?: { provider: string; displayName: string; requiresKey: boolean };
}

const ENDPOINT_PURPOSE =
  "An endpoint is where I reach a model: a server on this machine or a hosted provider. I can't do anything until I have one.";

function endpointFinding(s: EndpointBriefing): string {
  if (s.form)
    return s.form.requiresKey
      ? `Paste your ${s.form.displayName} API key; I keep it encrypted and test the connection when you save.`
      : "Enter the server's address, ending in `/v1`; I'll test it when you save.";
  if (s.latest?.ok)
    return `${s.latest.name} answered with ${count(s.latest.models, "model")}. Continue, or add a second endpoint as a fallback.`;
  if (s.latest)
    return [
      `${s.latest.name} didn't answer.`,
      s.latest.detail,
      "Check the server and retest, or pick another.",
    ]
      .filter(Boolean)
      .join(" ");
  if (s.scanning) return "I'm checking this machine for one now.";
  const [first] = s.available;
  if (!first)
    return "Nothing is running on this machine, so point me at a server or a provider.";
  const loaded = loadedModel(first);
  return loaded
    ? `${first.label} is running here with \`${loaded.id}\` loaded. I'd connect it: nothing you send leaves this machine.`
    : `${first.label} is running here with ${count(first.models.length, "model")}. I'd connect it: nothing you send leaves this machine.`;
}

export function endpointBriefing(s: EndpointBriefing): string {
  return join(ENDPOINT_PURPOSE, endpointFinding(s));
}

export interface ChatBriefing {
  picked?: string;
  pickedWindow: number | null;
  pickedLoaded: boolean;
  needsWindow: boolean;
}

const CHAT_PURPOSE =
  "The chat model answers you, and it's the one I think and use tools with.";

export function chatBriefing(s: ChatBriefing): string {
  if (!s.picked)
    return join(
      CHAT_PURPOSE,
      "No working endpoint lists a model yet; go back and check it.",
    );
  if (s.needsWindow)
    return join(
      CHAT_PURPOSE,
      `\`${s.picked}\` doesn't report its context window. Enter the one the server runs with, so I know when a conversation is getting too long.`,
    );
  const window = s.pickedWindow
    ? `, with a ${formatTokens(s.pickedWindow)} window`
    : "";
  return join(
    CHAT_PURPOSE,
    s.pickedLoaded
      ? `I picked \`${s.picked}\` because it's already loaded${window}.`
      : `I picked \`${s.picked}\`${window}.`,
  );
}

export function rolesBriefing(embedder?: {
  server: string;
  model: string;
}): string {
  return join(
    "Helper models take the side jobs: background work like titles, a reviewer for Auto, and recall across past conversations. Each default works.",
    embedder &&
      `One is worth changing: \`${embedder.model}\` on ${embedder.server} lets me recall by meaning, not just keywords.`,
  );
}

export function workspaceBriefing(): string {
  return "How the workspace looks, and where I search the web. I search through my own SearXNG unless you give me yours. Both can change later in Settings.";
}

export function readyBriefing(): string {
  return "That's everything. Here's what I'll run on; edit any line, or enter the workspace.";
}

/** The one-line reason under a provider row. */
export function providerReason(id: string, requiresKey: boolean): string {
  switch (id) {
    case "openai-compatible":
      return "Any server that speaks the OpenAI API: vLLM, llama-server, a gateway.";
    case "anthropic":
      return "Hosted Claude models. Needs an API key; billed per token.";
    case "google":
      return "Hosted Gemini models. Needs an API key; billed per token.";
    default:
      return requiresKey ? "Hosted. Needs an API key." : "Reached by URL.";
  }
}

/** The one-line reason under a chat-model row. */
export function modelReason(m: LocalModel | undefined): string {
  if (!m) return "Listed by the endpoint.";
  if (m.loaded) return "Loaded now: answers without a warm-up.";
  if (m.contextWindow)
    return `${formatTokens(m.contextWindow)} window; loads on first use.`;
  return "Loads on first use.";
}
