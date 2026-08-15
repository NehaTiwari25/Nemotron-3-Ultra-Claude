/**
 * Review files or git changes from the command line.
 *
 *   node scripts/review-file.mjs src/openrouter.ts
 *   node scripts/review-file.mjs src/review.ts src/sources.ts
 *   node scripts/review-file.mjs --git staged
 *   node scripts/review-file.mjs --git HEAD~1 --focus "error handling"
 *
 * The server reads the code itself, so nothing is loaded here.
 */
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { fileURLToPath } from "node:url";
import { dirname, join, extname } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

const argv = process.argv.slice(2);
const flag = (name) => {
  const i = argv.indexOf(`--${name}`);
  return i === -1 ? undefined : argv[i + 1];
};

const gitRef = flag("git");
const focus = flag("focus");
const paths = argv.filter((arg, i) => {
  if (arg.startsWith("--")) return false;
  return !argv[i - 1]?.startsWith("--");
});

if (!gitRef && paths.length === 0) {
  console.error(
    "Usage:\n" +
      "  node scripts/review-file.mjs <path> [<path>...] [--focus \"...\"]\n" +
      "  node scripts/review-file.mjs --git <ref|staged|unstaged> [--focus \"...\"]",
  );
  process.exit(1);
}

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

console.log(`Reviewing ${gitRef ? `git diff ${gitRef}` : paths.join(", ")}...\n`);
const started = Date.now();

const response = await client.callTool(
  {
    name: "review_diff",
    arguments: {
      ...(gitRef ? { git_ref: gitRef } : { paths }),
      cwd: process.cwd(),
      language: paths.length ? LANGUAGES[extname(paths[0])] : undefined,
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
