import { describe, expect, test } from "bun:test";
import {
  chatBriefing,
  endpointBriefing,
  loadedModel,
  modelReason,
  providerReason,
  readyBriefing,
  rolesBriefing,
  workspaceBriefing,
  type ChatBriefing,
  type EndpointBriefing,
} from "./briefings";
import type { LocalModel, LocalServer } from "./model";

/**
 * Talos's per-step briefings are assembled from optional state, so a missing branch
 * shows as a stray "undefined", a doubled blank line or a wrong plural on a screen the
 * operator reads once. These pin that each state selects the right finding, that the
 * fixed purpose paragraph never changes, and that interpolation never leaks.
 */

const ENDPOINT_PURPOSE = "An endpoint is where I reach a model";
const CHAT_PURPOSE = "The chat model answers you";
const ROLES_PURPOSE = "These are side jobs";

const MAX_PARAGRAPHS = 2;
const MAX_LENGTH = 320;

function model(id: string, over: Partial<LocalModel> = {}): LocalModel {
  return {
    id,
    contextWindow: null,
    parallelSlots: null,
    loaded: null,
    kind: null,
    ...over,
  };
}

function server(models: LocalModel[], label = "LM Studio"): LocalServer {
  return { label, baseUrl: "http://localhost:1234/v1", models };
}

function endpointState(over: Partial<EndpointBriefing> = {}): EndpointBriefing {
  return { scanning: false, available: [], ...over };
}

function chatState(over: Partial<ChatBriefing> = {}): ChatBriefing {
  return {
    picked: "m1",
    pickedWindow: null,
    pickedLoaded: false,
    needsWindow: false,
    ...over,
  };
}

function expectClean(text: string) {
  expect(text).not.toContain("\n\n\n");
  expect(text).not.toMatch(/undefined|null|false/);
  expect(text.trim()).toBe(text);
}

function expectShort(text: string) {
  expect(text.split("\n\n").length).toBeLessThanOrEqual(MAX_PARAGRAPHS);
  expect(text.length).toBeLessThanOrEqual(MAX_LENGTH);
}

function firstParagraph(text: string): string {
  return text.split("\n\n")[0] ?? "";
}

function expectStablePurpose(texts: string[], purpose: string) {
  const [head, ...rest] = texts.map(firstParagraph);
  expect(head).toContain(purpose);
  for (const other of rest) expect(other).toBe(head);
}

describe("loadedModel", () => {
  test("returns the loaded chat model", () => {
    const s = server([model("a"), model("b", { loaded: true })]);
    expect(loadedModel(s)?.id).toBe("b");
  });

  test("skips a loaded embedding model", () => {
    const s = server([model("e", { loaded: true, kind: "embeddings" })]);
    expect(loadedModel(s)).toBeUndefined();
  });

  test("is undefined when nothing reports loaded", () => {
    expect(loadedModel(server([model("a", { loaded: null })]))).toBeUndefined();
  });
});

describe("endpointBriefing", () => {
  const states: EndpointBriefing[] = [
    endpointState({
      form: {
        provider: "anthropic",
        displayName: "Anthropic",
        requiresKey: true,
      },
    }),
    endpointState({
      form: { provider: "p", displayName: "P", requiresKey: false },
    }),
    endpointState({
      latest: { name: "Local", ok: true, models: 1, detail: null },
    }),
    endpointState({
      latest: { name: "Local", ok: false, models: 0, detail: "refused" },
    }),
    endpointState({ scanning: true }),
    endpointState(),
    endpointState({ available: [server([model("a", { loaded: true })])] }),
    endpointState({ available: [server([model("a")])] }),
  ];

  test("form with a key requirement names the provider and the key", () => {
    const text = endpointBriefing(states[0]!);
    expect(text).toContain(ENDPOINT_PURPOSE);
    expect(text).toContain("Paste your Anthropic API key");
    expect(text).toContain("encrypted");
    expectClean(text);
  });

  test("form without a key requirement asks for the address", () => {
    const text = endpointBriefing(states[1]!);
    expect(text).toContain("address");
    expect(text).toContain("`/v1`");
    expect(text).not.toContain("API key");
    expectClean(text);
  });

  test("the form wins over a latest result", () => {
    const text = endpointBriefing(
      endpointState({
        form: { provider: "p", displayName: "P", requiresKey: false },
        latest: { name: "Old", ok: true, models: 3, detail: null },
      }),
    );
    expect(text).not.toContain("Old");
  });

  test("latest ok with one model is singular", () => {
    const text = endpointBriefing(states[2]!);
    expect(text).toContain("Local answered with 1 model.");
    expect(text).not.toContain("1 models");
    expect(text).toContain("fallback");
    expectClean(text);
  });

  test("latest ok with many models is plural", () => {
    const text = endpointBriefing(
      endpointState({
        latest: { name: "Local", ok: true, models: 4, detail: null },
      }),
    );
    expect(text).toContain("4 models");
    expectClean(text);
  });

  test("latest failed carries the detail", () => {
    const text = endpointBriefing(states[3]!);
    expect(text).toContain("Local didn't answer.");
    expect(text).toContain("refused");
    expect(text).toContain("retest");
    expectClean(text);
  });

  test("latest failed without detail still reads cleanly", () => {
    const text = endpointBriefing(
      endpointState({
        latest: { name: "Local", ok: false, models: 0, detail: null },
      }),
    );
    expect(text).toContain("Local didn't answer. Check the server");
    expect(text).not.toContain("  ");
    expectClean(text);
  });

  test("a latest result outranks scanning", () => {
    const text = endpointBriefing(
      endpointState({
        scanning: true,
        latest: { name: "Local", ok: true, models: 2, detail: null },
      }),
    );
    expect(text).not.toContain("checking this machine");
  });

  test("scanning says it is checking this machine", () => {
    const text = endpointBriefing(states[4]!);
    expect(text).toContain("checking this machine");
    expectClean(text);
  });

  test("nothing available asks to be pointed at a server or provider", () => {
    const text = endpointBriefing(states[5]!);
    expect(text).toContain("Nothing is running on this machine");
    expect(text).toContain("provider");
    expectClean(text);
  });

  test("an available server with a loaded model names it", () => {
    const text = endpointBriefing(
      endpointState({
        available: [
          server([
            model("qwen", { loaded: true, contextWindow: 65536 }),
            model("other"),
          ]),
        ],
      }),
    );
    expect(text).toContain("LM Studio is running here with `qwen` loaded");
    expect(text).toContain("nothing you send leaves this machine");
    expect(text).not.toContain("2 models");
    expectClean(text);
  });

  test("an available server with nothing loaded counts its models", () => {
    const text = endpointBriefing(
      endpointState({
        available: [server([model("qwen"), model("other")])],
      }),
    );
    expect(text).toContain("LM Studio is running here with 2 models");
    expect(text).not.toContain("`qwen`");
    expectClean(text);
  });

  test("an available server with one model is singular", () => {
    const text = endpointBriefing(
      endpointState({ available: [server([model("qwen")])] }),
    );
    expect(text).toContain("with 1 model.");
    expect(text).not.toContain("1 models");
  });

  test("a loaded embedding model does not count as loaded", () => {
    const text = endpointBriefing(
      endpointState({
        available: [server([model("e", { loaded: true, kind: "embeddings" })])],
      }),
    );
    expect(text).toContain("with 1 model.");
    expect(text).not.toContain("loaded");
  });

  test("only the first available server is described", () => {
    const text = endpointBriefing(
      endpointState({
        available: [
          server([model("a")], "First"),
          server([model("b")], "Second"),
        ],
      }),
    );
    expect(text).toContain("First");
    expect(text).not.toContain("Second");
  });

  test("every state opens with the same purpose and stays short", () => {
    const texts = states.map(endpointBriefing);
    expectStablePurpose(texts, ENDPOINT_PURPOSE);
    for (const text of texts) {
      expectShort(text);
      expectClean(text);
    }
  });
});

describe("chatBriefing", () => {
  const states: ChatBriefing[] = [
    chatState({ picked: undefined }),
    chatState({ needsWindow: true }),
    chatState({ pickedLoaded: true, pickedWindow: 131072 }),
    chatState(),
  ];

  test("no pick says no endpoint lists a model", () => {
    const text = chatBriefing(states[0]!);
    expect(text).toContain(CHAT_PURPOSE);
    expect(text).toContain("No working endpoint lists a model");
    expectClean(text);
  });

  test("needing a window asks for it and names the pick", () => {
    const text = chatBriefing(states[1]!);
    expect(text).toContain("`m1` doesn't report its context window");
    expect(text).toContain("Enter");
    expectClean(text);
  });

  test("needing a window outranks the loaded wording", () => {
    const text = chatBriefing(
      chatState({ needsWindow: true, pickedLoaded: true }),
    );
    expect(text).not.toContain("already loaded");
  });

  test("a loaded pick says why it was chosen and shows the window", () => {
    const text = chatBriefing(states[2]!);
    expect(text).toContain("I picked `m1` because it's already loaded");
    expect(text).toContain("128K window");
    expectClean(text);
  });

  test("a loaded pick without a window omits the window clause", () => {
    const text = chatBriefing(chatState({ pickedLoaded: true }));
    expect(text).toContain("because it's already loaded.");
    expect(text).not.toContain("window");
  });

  test("an unloaded pick with a window shows it", () => {
    const text = chatBriefing(chatState({ pickedWindow: 65536 }));
    expect(text).toContain("I picked `m1`, with a 64K window.");
    expect(text).not.toContain("already loaded");
  });

  test("an unloaded pick without a window is just named", () => {
    const text = chatBriefing(states[3]!);
    expect(text).toContain("I picked `m1`.");
    expect(text).not.toContain("already loaded");
    expect(text).not.toContain("window");
    expectClean(text);
  });

  test("every state opens with the same purpose and stays short", () => {
    const texts = states.map(chatBriefing);
    expectStablePurpose(texts, CHAT_PURPOSE);
    for (const text of texts) {
      expectShort(text);
      expectClean(text);
    }
  });
});

describe("rolesBriefing", () => {
  test("with an embedder names the server and model", () => {
    const text = rolesBriefing({ server: "LM Studio", model: "nomic-embed" });
    expect(text).toContain(ROLES_PURPOSE);
    expect(text).toContain("`nomic-embed` on LM Studio");
    expect(text).toContain("recall by meaning");
    expectClean(text);
  });

  test("without an embedder leaves out the recommendation", () => {
    const text = rolesBriefing();
    expect(text).toContain(ROLES_PURPOSE);
    expect(text).toContain("Each default works");
    expect(text).not.toContain("`");
    expect(text).not.toContain("\n\n");
    expectClean(text);
  });

  test("both shapes open with the same purpose and stay short", () => {
    const texts = [
      rolesBriefing({ server: "LM Studio", model: "nomic-embed" }),
      rolesBriefing(),
    ];
    for (const text of texts) expectShort(text);
    expect(texts[0]!.startsWith(texts[1]!)).toBe(true);
  });
});

describe("workspaceBriefing", () => {
  test("is one clean paragraph naming both settings", () => {
    const text = workspaceBriefing();
    expect(text).toContain("web");
    expect(text).toContain("Settings");
    expectShort(text);
    expect(text).not.toContain("\n\n");
    expectClean(text);
  });
});

describe("readyBriefing", () => {
  test("is one clean paragraph pointing at the summary", () => {
    const text = readyBriefing();
    expect(text).toContain("That's everything");
    expect(text).toContain("enter the workspace");
    expectShort(text);
    expect(text).not.toContain("\n\n");
    expectClean(text);
  });
});

describe("providerReason", () => {
  test("known providers have their own reason", () => {
    expect(providerReason("openai-compatible", false)).toContain("OpenAI API");
    expect(providerReason("anthropic", true)).toContain("Claude");
    expect(providerReason("google", true)).toContain("Gemini");
  });

  test("an unknown provider that needs a key says so", () => {
    expect(providerReason("other", true)).toContain("API key");
  });

  test("an unknown provider without a key is reached by URL", () => {
    expect(providerReason("other", false)).toContain("URL");
  });
});

describe("modelReason", () => {
  test("an unscanned model is listed by the endpoint", () => {
    expect(modelReason(undefined)).toContain("Listed by the endpoint");
  });

  test("a loaded model answers without a warm-up", () => {
    expect(
      modelReason(model("a", { loaded: true, contextWindow: 4096 })),
    ).toContain("Loaded now");
  });

  test("a model with a window shows it", () => {
    const text = modelReason(model("a", { contextWindow: 65536 }));
    expect(text).toContain("64K window");
    expect(text).toContain("loads on first use");
  });

  test("a model with neither loads on first use", () => {
    const text = modelReason(model("a"));
    expect(text).toContain("Loads on first use");
    expect(text).not.toContain("window");
  });
});
