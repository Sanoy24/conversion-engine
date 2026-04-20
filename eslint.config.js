// eslint.config.js — Tenacious Conversion Engine
//
// ESLint 9+ flat config. Pragmatic TS/JS linting.
// Install: npm install -D eslint @typescript-eslint/parser @typescript-eslint/eslint-plugin eslint-plugin-simple-import-sort

import tsParser from "@typescript-eslint/parser";
import tsPlugin from "@typescript-eslint/eslint-plugin";
import simpleImportSort from "eslint-plugin-simple-import-sort";

export default [
  {
    ignores: [
      "node_modules/**",
      "dist/**",
      "build/**",
      ".next/**",
      "coverage/**",
      "seeds_placeholder/**",
      "data/**",
      "traces/**",
      "*.min.js",
    ],
  },
  {
    files: ["**/*.{js,jsx,ts,tsx}"],
    languageOptions: {
      parser: tsParser,
      parserOptions: {
        ecmaVersion: "latest",
        sourceType: "module",
        ecmaFeatures: { jsx: true },
      },
    },
    plugins: {
      "@typescript-eslint": tsPlugin,
      "simple-import-sort": simpleImportSort,
    },
    rules: {
      // --- Import hygiene ---
      "simple-import-sort/imports": "error",
      "simple-import-sort/exports": "error",
      "no-duplicate-imports": "error",

      // --- Unused code ---
      "no-unused-vars": "off", // Turned off in favor of the TS-aware rule below
      "@typescript-eslint/no-unused-vars": [
        "warn",
        {
          argsIgnorePattern: "^_",
          varsIgnorePattern: "^_",
          caughtErrorsIgnorePattern: "^_",
        },
      ],

      // --- Type safety ---
      "@typescript-eslint/no-explicit-any": "warn",
      "@typescript-eslint/no-non-null-assertion": "warn",
      "@typescript-eslint/consistent-type-imports": [
        "warn",
        { prefer: "type-imports", fixStyle: "inline-type-imports" },
      ],

      // --- Correctness ---
      "no-console": ["warn", { allow: ["warn", "error"] }],
      "no-debugger": "error",
      "no-eval": "error",
      "no-implied-eval": "error",
      "prefer-const": "error",
      "no-var": "error",
      eqeqeq: ["error", "smart"],

      // --- Promise handling ---
      "no-async-promise-executor": "error",
      "require-await": "warn",

      // --- Style (formatter handles most; a few things formatter doesn't) ---
      curly: ["error", "multi-line"],
      "object-shorthand": "warn",
    },
  },
  {
    // Tests: relaxed
    files: ["**/*.test.{js,ts,jsx,tsx}", "**/*.spec.{js,ts,jsx,tsx}", "tests/**"],
    rules: {
      "@typescript-eslint/no-explicit-any": "off",
      "@typescript-eslint/no-non-null-assertion": "off",
      "no-console": "off",
    },
  },
];
