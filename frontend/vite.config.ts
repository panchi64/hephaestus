import { defineConfig } from "vite";
import { nitro } from "nitro/vite";
import { solidStart } from "@solidjs/start/config";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [solidStart({ ssr: false }), tailwindcss(), nitro({ preset: "node-server" })],
  assetsInclude: ["**/*.wasm"],
  // The View's PDF renderer resolves pdf.js's worker through a dynamic import, which
  // Rollup cannot bundle into a classic iife worker. See renderers/PdfViewer.tsx.
  worker: { format: "es" },
});
