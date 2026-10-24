import { defineConfig } from "eslint/config";
import tseslint from "typescript-eslint";
import solid from "eslint-plugin-solid/configs/typescript";

export default defineConfig(
  {
    ignores: [
      "node_modules/**",
      ".nitro/**",
      ".output/**",
      "dist/**",
      "public/**",
    ],
  },
  ...tseslint.configs.recommended,
  {
    files: ["src/**/*.{ts,tsx}"],
    ...solid,
    rules: {
      // Underscore-prefixed args/vars are intentional placeholders.
      "@typescript-eslint/no-unused-vars": [
        "warn",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  },
);
