/**
 * Runs Second Opinion against demo/src/cache.ts, which contains five
 * deliberately planted defects (ground truth in demo/PLANTED_BUGS.md).
 *
 * Writes the report to demo/RESULT.md so it can be scored and screenshotted.
 *
 *   OPENROUTER_API_KEY=sk-or-v1-... node demo/run-demo.mjs
 */
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");

if (!process.env.OPENROUTER_API_KEY) {
  console.error(
    "OPENROUTER_API_KEY is not set.\n" +
      "Get a key at https://openrouter.ai/keys, then re-run.",
  );
  process.exit(1);
}

const source = await readFile(join(here, "src", "cache.ts"), "utf8");

// Sent as a whole file rather than a diff: this is new code, and it is how the
// tool gets used in practice when reviewing something freshly written.
const transport = new StdioClientTransport({
  command: "node",
  args: [join(root, "dist", "index.js")],
  env: { ...process.env },
});

const client = new Client({ name: "second-opinion-demo", version: "0.1.0" });

console.log("Connecting to second-opinion...");
await client.connect(transport);

const model = process.env.SECOND_OPINION_MODEL ?? "nvidia/nemotron-3-ultra-550b-a55b:free";
console.log(`Reviewing demo/src/cache.ts with ${model}`);
console.log("A 550B reasoning model takes a minute or two. Waiting...\n");

const started = Date.now();

const response = await client.callTool(
  {
    name: "review_diff",
    arguments: {
      diff: `// File: src/cache.ts (new)\n\n${source}`,
      language: "TypeScript",
      focus:
        "correctness under concurrency, batching boundaries, and long-running " +
        "cache state",
    },
  },
  undefined,
  { timeout: 300_000 },
);

const elapsed = ((Date.now() - started) / 1000).toFixed(1);
const report = response.content?.[0]?.text ?? "(no content returned)";

if (response.isError) {
  console.error(`\nReview failed after ${elapsed}s:\n${report}`);
  await client.close();
  process.exit(1);
}

console.log(report);
console.log(`\n---\nCompleted in ${elapsed}s.`);

const output = [
  "# Demo result",
  "",
  `Reviewer: \`${model}\`  ·  Elapsed: ${elapsed}s  ·  Run: ${new Date().toISOString()}`,
  "",
  "Target: `demo/src/cache.ts` — 5 planted defects, ground truth in `PLANTED_BUGS.md`.",
  "",
  "---",
  "",
  report,
  "",
  "---",
  "",
  "## Score",
  "",
  "| # | Planted bug | Found? |",
  "|---|---|---|",
  "| 1 | Off-by-one in `chunk()` | |",
  "| 2 | `forEach` with async callback | |",
  "| 3 | Swallowed upstream error | |",
  "| 4 | `size` never decremented | |",
  "| 5 | No request coalescing | |",
  "",
  "False positives: ",
  "",
].join("\n");

await writeFile(join(here, "RESULT.md"), output, "utf8");
console.log("Report written to demo/RESULT.md — fill in the score table.");

await client.close();
