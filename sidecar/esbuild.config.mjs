// Sidecar bundler config.
//
// Bundles src/index.ts -> dist/index.js as a Node ESM module. Marks the SDK
// (and its sub-packages) as `external` so they resolve from node_modules at
// runtime — the SDK ships native binary optional deps that must NOT be
// inlined into the bundle. The CommonJS-interop banner lets the resulting
// ESM bundle pull in CJS dependencies via createRequire.
import { build } from "esbuild";

await build({
  entryPoints: ["src/index.ts"],
  bundle: true,
  outfile: "dist/index.js",
  platform: "node",
  target: "node20",
  format: "esm",
  external: [
    "@anthropic-ai/claude-agent-sdk",
    "@anthropic-ai/claude-agent-sdk/*",
  ],
  banner: {
    js: "import { createRequire } from 'module'; const require = createRequire(import.meta.url);",
  },
  logLevel: "info",
});
