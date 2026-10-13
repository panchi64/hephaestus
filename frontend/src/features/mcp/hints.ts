/** The two pieces of MCP jargon the operator has to be told, once, wherever it
 *  appears. Both the server card and the register dialog show them, so they live
 *  here rather than being retyped — a hint that disagrees with itself between two
 *  surfaces is worse than no hint. */

export const TRANSPORT_HINT =
  "STDIO runs the server as a local subprocess and talks over stdin/stdout — best for tools on this machine. HTTP connects to a server over the network at a URL — use it for remote or shared servers. SSE is the older network transport, for servers that predate Streamable HTTP.";

export const TRUST_HINT =
  "An external tool's effects aren't knowable to Hephaestus, so every call pauses for your approval by default. Trust one you've vetted to let it run without asking — one tool at a time, and revocable at any moment.";
