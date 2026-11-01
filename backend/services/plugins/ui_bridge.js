// The bridge SDK a plugin window loads with <script src="__bridge.js"></script>.
//
// The host hands the frame one MessagePort on load; every call and event goes over it.
// `odysseus.ready` resolves with {plugin, window, permissions} once it has arrived, and
// calls made before then wait for it.
(() => {
  "use strict";
  if (window.odysseus) return;

  let port = null;
  let nextId = 1;
  const pending = new Map();
  const listeners = new Map();
  let resolveReady;
  const ready = new Promise((resolve) => {
    resolveReady = resolve;
  });

  window.addEventListener("message", (event) => {
    const data = event.data;
    if (port || event.source !== window.parent) return;
    if (!data || data.odysseus !== "init" || !event.ports[0]) return;
    port = event.ports[0];
    port.onmessage = (message) => {
      const reply = message.data;
      if (reply && typeof reply.event === "string") {
        for (const fn of listeners.get(reply.event) || []) {
          try {
            fn(reply.data);
          } catch (err) {
            console.error(err);
          }
        }
        return;
      }
      const waiting = reply && pending.get(reply.id);
      if (!waiting) return;
      pending.delete(reply.id);
      if (reply.ok) waiting.resolve(reply.result);
      else {
        const error = new Error(reply.error.message);
        error.code = reply.error.code;
        waiting.reject(error);
      }
    };
    resolveReady(
      Object.freeze({
        plugin: data.plugin,
        window: data.window,
        permissions: Object.freeze([...data.permissions]),
      }),
    );
  });

  async function call(method, params) {
    await ready;
    const id = nextId++;
    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject });
      port.postMessage({ id, method, params: params || {} });
    });
  }

  function on(event, fn) {
    if (!listeners.has(event)) listeners.set(event, new Set());
    listeners.get(event).add(fn);
    return () => listeners.get(event).delete(fn);
  }

  window.odysseus = Object.freeze({
    ready,
    call,
    on,
    storage: Object.freeze({
      get: (key) => call("storage.get", { key }),
      set: (key, value) => call("storage.set", { key, value }),
      delete: (key) => call("storage.delete", { key }),
      list: () => call("storage.list"),
    }),
    tools: Object.freeze({
      call: (name, args) => call("tools.call", { name, args: args || {} }),
    }),
    chat: Object.freeze({
      open: (prompt) => call("chat.open", { prompt }),
      post: (prompt, conversationId) =>
        call("chat.post", { prompt, conversation_id: conversationId || null }),
    }),
    window: Object.freeze({
      close: () => call("window.close"),
    }),
  });
})();
