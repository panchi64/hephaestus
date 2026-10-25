/** Compiles `.tsx` with Solid's JSX compiler under `bun test`.
 *
 *  Bun's built-in transform emits React-style `jsxDEV` calls, which Solid does not
 *  implement. This mirrors vite-plugin-solid: Babel parses the TypeScript but only
 *  rewrites the JSX, and Bun strips the types afterwards. */

import { transformAsync } from "@babel/core";
import { plugin } from "bun";

plugin({
  name: "solid-jsx",
  setup(build) {
    build.onLoad({ filter: /\.tsx$/ }, async ({ path }) => {
      const result = await transformAsync(await Bun.file(path).text(), {
        filename: path,
        babelrc: false,
        configFile: false,
        parserOpts: { plugins: ["jsx", "typescript"] },
        // Bun resolves `solid-js/web` through its "node" condition to the server
        // build, so the output must target it; DOM output throws at import.
        presets: [
          ["babel-preset-solid", { generate: "ssr", hydratable: false }],
        ],
      });
      if (result?.code == null)
        throw new Error(`Babel produced no code for ${path}`);
      return { contents: result.code, loader: "ts" };
    });
  },
});
