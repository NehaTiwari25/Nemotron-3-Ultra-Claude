/**
 * Verifies the client spaces requests out even when callers arrive
 * concurrently. MCP clients can invoke tools in parallel, so a throttle that
 * only works for sequential callers is not a throttle.
 *
 *   node scripts/test-throttle.mjs
 */
const CONCURRENT_CALLS = 4;
const MIN_SPACING_MS = 3_000;

const timestamps = [];

// Stub the network before importing the client, so nothing leaves the machine.
globalThis.fetch = async () => {
  timestamps.push(Date.now());
  return {
    ok: true,
    status: 200,
    json: async () => ({ choices: [{ message: { content: "{}" } }] }),
    text: async () => "",
  };
};

process.env.OPENROUTER_API_KEY = "test-key-not-real";

const { complete } = await import("../dist/openrouter.js");

const call = () =>
  complete({ messages: [{ role: "user", content: "ping" }] });

console.log(`Firing ${CONCURRENT_CALLS} concurrent requests...`);
const started = Date.now();
await Promise.all(Array.from({ length: CONCURRENT_CALLS }, call));

const offsets = timestamps.map((t) => t - started);
console.log(`Request offsets (ms): ${offsets.join(", ")}`);

let failed = false;
for (let i = 1; i < timestamps.length; i++) {
  const gap = timestamps[i] - timestamps[i - 1];
  if (gap < MIN_SPACING_MS) {
    console.log(`FAIL  requests ${i} and ${i + 1} were ${gap}ms apart (need >=${MIN_SPACING_MS}ms)`);
    failed = true;
  }
}

if (timestamps.length !== CONCURRENT_CALLS) {
  console.log(`FAIL  expected ${CONCURRENT_CALLS} requests, saw ${timestamps.length}`);
  failed = true;
}

if (!failed) {
  console.log(`PASS  all ${CONCURRENT_CALLS} requests spaced >=${MIN_SPACING_MS}ms apart`);
}

process.exit(failed ? 1 : 0);
