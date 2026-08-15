/**
 * Review any file from the command line.
 *
 *   node scripts/review-file.mjs src/openrouter.ts
 *   node scripts/review-file.mjs src/review.ts --focus "prompt injection"
 *
 * Reads OPENROUTER_API_KEY from the server's own .env.
 */
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve, extname } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

const args = process.argv.slice(2);
const target = args.find((a) => !a.startsWith("--"));
const focusFlag = args.indexOf("--focus");
const focus = focusFlag !== -1 ? args[focusFlag + 1] : undefined;

if (!target) {
  console.error("Usage: node scripts/review-file.mjs <path> [--focus \"...\"]");
  process.exit(1);
}

const path = resolve(target);
const source = await readFile(path, "utf8");

const LANGUAGES = {
  ".ts": "TypeScript",
  ".tsx": "TypeScript",
  ".js": "JavaScript",
  ".mjs": "JavaScript",
  ".py": "Python",
  ".cs": "C#",
  ".go": "Go",
  ".rs": "Rust",
};

const transport = new StdioClientTransport({
  command: "node",
  args: [join(root, "dist", "index.js")],
  env: { ...process.env },
});

const client = new Client({ name: "review-file", version: "0.1.0" });
await client.connect(transport);

console.log(`Reviewing ${target} (${source.split("\n").length} lines)...\n`);
const started = Date.now();

const response = await client.callTool(
  {
    name: "review_diff",
    arguments: {
      diff: `// File: ${target}\n\n${source}`,
      language: LANGUAGES[extname(path)],
      focus,
    },
  },
  undefined,
  { timeout: 300_000 },
);

console.log(response.content?.[0]?.text ?? "(no content)");
console.log(`\n---\n${((Date.now() - started) / 1000).toFixed(1)}s`);

await client.close();
process.exit(response.isError ? 1 : 0);
