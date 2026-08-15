import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

/**
 * Loads a .env file sitting next to the package, without pulling in dotenv.
 *
 * MCP clients launch the server as a subprocess, so the usual "export it in
 * your shell" advice doesn't apply — the variable has to reach the child
 * somehow. Supporting a local .env means the client config doesn't need the
 * key inlined, which keeps secrets out of files people commit by reflex.
 *
 * Real environment variables always win, so an MCP config `env` block or a
 * CI secret still overrides the file.
 */
export function loadDotEnv(): void {
  const packageRoot = join(dirname(fileURLToPath(import.meta.url)), "..");

  let contents: string;
  try {
    contents = readFileSync(join(packageRoot, ".env"), "utf8");
  } catch {
    return; // No .env is perfectly normal.
  }

  for (const line of contents.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;

    const separator = trimmed.indexOf("=");
    if (separator === -1) continue;

    const key = trimmed.slice(0, separator).trim();
    if (!key || process.env[key] !== undefined) continue;

    let value = trimmed.slice(separator + 1).trim();
    const quoted =
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"));
    if (quoted && value.length >= 2) value = value.slice(1, -1);

    process.env[key] = value;
  }
}
