/**
 * Smoke test: starts the server over stdio and checks it completes the MCP
 * handshake and advertises its tools. Does not call OpenRouter.
 *
 *   node scripts/smoke.mjs
 */
import { spawn } from "node:child_process";

const child = spawn("node", ["dist/index.js"], {
  stdio: ["pipe", "pipe", "pipe"],
  env: { ...process.env, OPENROUTER_API_KEY: "smoke-test-not-a-real-key" },
});

let buffer = "";
const pending = new Map();

child.stdout.on("data", (chunk) => {
  buffer += chunk.toString();
  let newline;
  while ((newline = buffer.indexOf("\n")) !== -1) {
    const line = buffer.slice(0, newline).trim();
    buffer = buffer.slice(newline + 1);
    if (!line) continue;
    const message = JSON.parse(line);
    const resolve = pending.get(message.id);
    if (resolve) {
      pending.delete(message.id);
      resolve(message);
    }
  }
});

child.stderr.on("data", (d) => process.stderr.write(`[server] ${d}`));

let nextId = 1;
function request(method, params = {}) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    pending.set(id, resolve);
    child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
    setTimeout(() => reject(new Error(`timeout waiting for ${method}`)), 10_000);
  });
}

function notify(method, params = {}) {
  child.stdin.write(JSON.stringify({ jsonrpc: "2.0", method, params }) + "\n");
}

const check = (label, ok) => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}`);
  if (!ok) process.exitCode = 1;
};

try {
  const init = await request("initialize", {
    protocolVersion: "2024-11-05",
    capabilities: {},
    clientInfo: { name: "smoke", version: "0.0.0" },
  });
  check("handshake completes", init.result?.serverInfo?.name === "second-opinion");

  notify("notifications/initialized");

  const list = await request("tools/list");
  const tools = list.result?.tools ?? [];
  check("advertises review_diff", tools.some((t) => t.name === "review_diff"));

  const tool = tools.find((t) => t.name === "review_diff");
  const props = tool?.inputSchema?.properties ?? {};
  for (const field of ["paths", "git_ref", "cwd", "diff", "context", "focus"]) {
    check(`${field} is a declared input`, field in props);
  }
  check(
    "path sources are steered toward in the description",
    /paths|git_ref/.test(tool?.description ?? ""),
  );

  // Omitting every source must be a clean error, not a crash or an empty review.
  const noSource = await request("tools/call", {
    name: "review_diff",
    arguments: {},
  });
  check("missing source is rejected", noSource.result?.isError === true);
  check(
    "missing-source message names the options",
    /paths.*git_ref|git_ref.*paths/s.test(noSource.result?.content?.[0]?.text ?? ""),
  );

  // A bad key must surface as a tool error, not crash the server.
  const call = await request("tools/call", {
    name: "review_diff",
    arguments: { diff: "- const a = 1;\n+ const a = 2;" },
  });
  const text = call.result?.content?.[0]?.text ?? "";
  check("bad credentials return an error, not a crash", call.result?.isError === true);
  check("error message is actionable", /API key|401/i.test(text));
} catch (error) {
  console.error("FAIL ", error.message);
  process.exitCode = 1;
} finally {
  child.kill();
}
