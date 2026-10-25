import { formatTokens, type LocalModel, type LocalServer } from "./model";

/** Talos's briefing for each step, written as Markdown so model names and paths render
 *  in the machine voice. Each one says what the step is for, what Talos found or did,
 *  what it recommends and why, and what happens next — and is rewritten as the state
 *  changes, so it never describes a screen the operator is no longer looking at. */

const para = (...lines: (string | false | null | undefined)[]): string =>
  lines.filter(Boolean).join(" ");
const join = (...paragraphs: (string | false | null | undefined)[]): string =>
  paragraphs.filter(Boolean).join("\n\n");

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

export function endpointBriefing(s: EndpointBriefing): string {
  const opener =
    "Everything I do runs through a model, so this is the one part of setup I can't do without you.";

  if (s.form)
    return join(
      opener,
      s.form.requiresKey
        ? para(
            `Paste the API key from your ${s.form.displayName} console; the address is filled in for you.`,
            "I store the key encrypted and never show it again, and I'll test the connection as soon as you save.",
          )
        : para(
            "Give me the server's base URL, ending in `/v1`.",
            "A key is only needed if the server asks for one. I'll test the connection as soon as you save.",
          ),
    );

  if (s.latest?.ok)
    return join(
      opener,
      para(
        `${s.latest.name} answered and lists ${s.latest.models} ${s.latest.models === 1 ? "model" : "models"}.`,
        "That's all I need from you here.",
      ),
      "Add another endpoint if you want a fallback for when this one is down; otherwise, continue and pick the model you'll talk to.",
    );

  if (s.latest && !s.latest.ok)
    return join(
      opener,
      para(
        `${s.latest.name} didn't pass the connection test.`,
        s.latest.detail ?? "",
      ),
      "Check that the server is running and the address is right, then retest. Or choose a different provider.",
    );

  if (s.scanning)
    return join(
      opener,
      "I'm checking this machine for a model server first, so you don't have to type an address you already have running.",
    );

  const [first] = s.available;
  if (!first)
    return join(
      opener,
      para(
        "Nothing is answering on this machine's usual ports: LM Studio on `1234`, llama-server on `8080`.",
        "Start one and reopen this step, or point me at a server elsewhere or a hosted provider.",
      ),
      "Hosted providers need an API key and bill per token; a local server keeps everything on this machine.",
    );

  const loaded = loadedModel(first);
  return join(
    opener,
    para(
      `${first.label} is running on this machine with ${first.models.length} ${first.models.length === 1 ? "model" : "models"}.`,
      loaded
        ? `\`${loaded.id}\` is already loaded${loaded.contextWindow ? ` with a ${formatTokens(loaded.contextWindow)} window` : ""}, so the first answer has no warm-up and nothing you send leaves this machine.`
        : "Connecting it keeps everything you send on this machine.",
    ),
    "That's what I'd start with. If you'd rather use a hosted model, pick its provider and I'll ask for the key.",
  );
}

export interface ChatBriefing {
  picked?: string;
  pickedWindow: number | null;
  pickedLoaded: boolean;
  choices: number;
  needsWindow: boolean;
}

export function chatBriefing(s: ChatBriefing): string {
  const opener =
    "This is the model you'll talk to, and the one I reason and call tools with.";
  if (!s.picked)
    return join(
      opener,
      "No working endpoint lists a model yet. Go back and check the endpoint; I'll pick up whatever it serves.",
    );
  if (s.needsWindow)
    return join(
      opener,
      para(
        `\`${s.picked}\` doesn't report its context window, and I need it to know when a conversation is about to outgrow what the model can hold.`,
        "Enter the window the server was started with — for llama-server, its `-c` value.",
      ),
    );
  return join(
    opener,
    para(
      s.pickedLoaded
        ? `I've picked \`${s.picked}\` because it's already loaded, so there's no warm-up before the first answer.`
        : `I've picked \`${s.picked}\`.`,
      s.pickedWindow
        ? `Its ${formatTokens(s.pickedWindow)} window is how much conversation and file content I can keep in view at once.`
        : "",
    ),
    s.choices > 1 &&
      "Any of the others work too: a larger window helps with long files, a smaller model answers faster. You can switch any time from the composer.",
  );
}

export function rolesBriefing(embedder?: {
  server: string;
  model: string;
}): string {
  return join(
    "Each of these has a default that works. Keep it unless one matters to you now; all three live in Settings › Models.",
    embedder
      ? `The one I'd change is **Search & memory**: ${embedder.server} serves \`${embedder.model}\`, an embedding model, and with it I can recall past conversations by meaning rather than by keyword.`
      : "Leaving **Search & memory** on keywords is fine to start; recall by meaning needs an embedding model, which none of your endpoints lists yet.",
  );
}

export function workspaceBriefing(): string {
  return join(
    "Two preferences are worth setting now: how the workspace looks, and where I search the web. Both live in Settings, so you can change either later.",
    "Web search needs a SearXNG instance. Without one, I work only from what you give me and the pages you link.",
  );
}

export function readyBriefing(): string {
  return join(
    "Everything I need is in place. This is what I'll be running on; anything you want to change is one click back.",
    "Enter when you're ready; you'll land on the home screen.",
  );
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
