import { describe, expect, test } from "bun:test";
import {
  chatBriefing,
  endpointBriefing,
  loadedModel,
  modelReason,
  providerReason,
  rolesBriefing,
  type ChatBriefing,
  type EndpointBriefing,
} from "./briefings";
import type { LocalModel, LocalServer } from "./model";

/**
 * Talos's per-step briefings are assembled from optional state, so a missing branch
 * shows as a stray "undefined", a doubled blank line or a wrong plural on a screen the
 * operator reads once. These pin that each state selects the right paragraph and that
 * interpolation never leaks.
 */

const OPENER = "Everything I do runs through a model";

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
    choices: 1,
    needsWindow: false,
    ...over,
  };
}

function expectClean(text: string) {
  expect(text).not.toContain("\n\n\n");
  expect(text).not.toMatch(/undefined|null|false/);
  expect(text.trim()).toBe(text);
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
  test("form with a key requirement names the provider and the key", () => {
    const text = endpointBriefing(
      endpointState({
        form: {
          provider: "anthropic",
          displayName: "Anthropic",
          requiresKey: true,
        },
      }),
    );
    expect(text).toContain(OPENER);
    expect(text).toContain("Anthropic console");
    expect(text).toContain("API key");
    expectClean(text);
  });

  test("form without a key requirement asks for the base URL", () => {
    const text = endpointBriefing(
      endpointState({
        form: {
          provider: "openai-compatible",
          displayName: "OpenAI-compatible",
          requiresKey: false,
        },
      }),
    );
    expect(text).toContain(OPENER);
    expect(text).toContain("base URL");
    expect(text).toContain("`/v1`");
    expect(text).not.toContain("console");
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
    const text = endpointBriefing(
      endpointState({
        latest: { name: "Local", ok: true, models: 1, detail: null },
      }),
    );
    expect(text).toContain(OPENER);
    expect(text).toContain("Local answered");
    expect(text).toContain("1 model.");
    expect(text).not.toContain("1 models");
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
    const text = endpointBriefing(
      endpointState({
        latest: {
          name: "Local",
          ok: false,
          models: 0,
          detail: "connection refused",
        },
      }),
    );
    expect(text).toContain(OPENER);
    expect(text).toContain("Local didn't pass");
    expect(text).toContain("connection refused");
    expectClean(text);
  });

  test("latest failed without detail still reads cleanly", () => {
    const text = endpointBriefing(
      endpointState({
        latest: { name: "Local", ok: false, models: 0, detail: null },
      }),
    );
    expect(text).toContain("Local didn't pass");
    expectClean(text);
  });

  test("scanning says it is checking this machine", () => {
    const text = endpointBriefing(endpointState({ scanning: true }));
    expect(text).toContain(OPENER);
    expect(text).toContain("checking this machine");
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

  test("nothing available names the usual ports", () => {
    const text = endpointBriefing(endpointState());
    expect(text).toContain(OPENER);
    expect(text).toContain("`1234`");
    expect(text).toContain("`8080`");
    expectClean(text);
  });

  test("an available server with a loaded model names it and its window", () => {
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
    expect(text).toContain(OPENER);
    expect(text).toContain("LM Studio is running");
    expect(text).toContain("2 models");
    expect(text).toContain("`qwen` is already loaded");
    expect(text).toContain("64K window");
    expectClean(text);
  });

  test("a loaded model without a window omits the window clause", () => {
    const text = endpointBriefing(
      endpointState({
        available: [server([model("qwen", { loaded: true })])],
      }),
    );
    expect(text).toContain("1 model.");
    expect(text).toContain("`qwen` is already loaded");
    expect(text).not.toContain("window");
    expectClean(text);
  });

  test("an available server with nothing loaded does not name a model", () => {
    const text = endpointBriefing(
      endpointState({
        available: [server([model("qwen", { contextWindow: 65536 })])],
      }),
    );
    expect(text).toContain("LM Studio is running");
    expect(text).not.toContain("`qwen`");
    expect(text).not.toContain("already loaded");
    expectClean(text);
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
});

describe("chatBriefing", () => {
  const opener = "the model you'll talk to";

  test("no pick says no endpoint lists a model", () => {
    const text = chatBriefing(chatState({ picked: undefined }));
    expect(text).toContain(opener);
    expect(text).toContain("No working endpoint lists a model");
    expectClean(text);
  });

  test("needing a window asks for it and names the pick", () => {
    const text = chatBriefing(chatState({ needsWindow: true }));
    expect(text).toContain(opener);
    expect(text).toContain("`m1` doesn't report its context window");
    expect(text).toContain("`-c`");
    expectClean(text);
  });

  test("a loaded pick says why it was chosen and shows the window", () => {
    const text = chatBriefing(
      chatState({ pickedLoaded: true, pickedWindow: 131072 }),
    );
    expect(text).toContain("`m1` because it's already loaded");
    expect(text).toContain("128K window");
    expectClean(text);
  });

  test("an unloaded pick without a window is just named", () => {
    const text = chatBriefing(chatState());
    expect(text).toContain("I've picked `m1`.");
    expect(text).not.toContain("already loaded");
    expect(text).not.toContain("window");
    expectClean(text);
  });

  test("a single choice does not mention alternatives", () => {
    expect(chatBriefing(chatState({ choices: 1 }))).not.toContain("the others");
  });

  test("multiple choices mention alternatives", () => {
    const text = chatBriefing(chatState({ choices: 3 }));
    expect(text).toContain("the others");
    expectClean(text);
  });
});

describe("rolesBriefing", () => {
  test("with an embedder names the server and model", () => {
    const text = rolesBriefing({ server: "LM Studio", model: "nomic-embed" });
    expect(text).toContain("Search & memory");
    expect(text).toContain("LM Studio");
    expect(text).toContain("`nomic-embed`");
    expectClean(text);
  });

  test("without an embedder explains keyword recall", () => {
    const text = rolesBriefing();
    expect(text).toContain("Search & memory");
    expect(text).toContain("keywords");
    expect(text).not.toContain("`");
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
