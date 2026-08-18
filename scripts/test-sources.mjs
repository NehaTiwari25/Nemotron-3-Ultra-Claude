/**
 * Unit tests for source resolution. No network, no API key needed.
 *
 *   node scripts/test-sources.mjs
 */
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const run = promisify(execFile);
const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const { resolveSource } = await import(
  new URL("../dist/sources.js", import.meta.url).href
);

let failures = 0;
const check = (label, ok, detail = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}${ok || !detail ? "" : ` — ${detail}`}`);
  if (!ok) failures++;
};

async function expectError(label, spec, pattern) {
  try {
    await resolveSource(spec);
    check(label, false, "expected an error, got none");
  } catch (error) {
    check(label, pattern.test(error.message), `message was: ${error.message}`);
  }
}

// --- source selection -------------------------------------------------------

await expectError("rejects no source", {}, /Provide one source/);
await expectError(
  "rejects two sources",
  { diff: "x", paths: ["y"] },
  /exactly one source/,
);

// --- literal diff -----------------------------------------------------------

const literal = await resolveSource({ diff: "- a\n+ b" });
check("literal diff passes through", literal.content === "- a\n+ b");
check("literal diff is described", literal.description === "supplied diff");

// --- paths ------------------------------------------------------------------

const scratch = await mkdtemp(join(tmpdir(), "second-opinion-test-"));
try {
  await writeFile(join(scratch, "a.ts"), "export const a = 1;\n");
  await writeFile(join(scratch, "b.ts"), "export const b = 2;\n");

  const single = await resolveSource({ paths: ["a.ts"], cwd: scratch });
  check("reads a file by relative path", single.content.includes("export const a = 1;"));
  check("labels the file in the content", single.content.includes("File: a.ts"));

  const multi = await resolveSource({ paths: ["a.ts", "b.ts"], cwd: scratch });
  check(
    "reads multiple files",
    multi.content.includes("const a = 1") && multi.content.includes("const b = 2"),
  );
  check("describes the file count", multi.description.startsWith("2 files"));
  check("reports byte size", multi.bytes > 0);
  // The description is echoed in the report header, so it should read back the
  // caller's own spellings rather than resolved absolute paths.
  check(
    "describes files as the caller named them",
    multi.description === "2 files: a.ts, b.ts",
    `description was: ${multi.description}`,
  );

  // The same file listed twice is read twice, and the reviewer pays for the
  // duplicate in context it already has. Relative and absolute spellings of one
  // path are the same file and should collapse too.
  const repeated = await resolveSource({ paths: ["a.ts", "a.ts"], cwd: scratch });
  check(
    "duplicate paths are read once",
    repeated.content.split("File: a.ts").length - 1 === 1,
    `file appeared ${repeated.content.split("File: a.ts").length - 1} times`,
  );
  const mixed = await resolveSource({
    paths: ["a.ts", join(scratch, "a.ts")],
    cwd: scratch,
  });
  check(
    "relative and absolute spellings collapse",
    mixed.content.split("export const a = 1;").length - 1 === 1,
    `content appeared ${mixed.content.split("export const a = 1;").length - 1} times`,
  );

  await expectError(
    "rejects a missing file",
    { paths: ["nope.ts"], cwd: scratch },
    /Cannot read/,
  );
  await expectError(
    "rejects a directory",
    { paths: ["."], cwd: scratch },
    /is a directory/,
  );

  // --- git ------------------------------------------------------------------

  await expectError(
    "rejects git outside a repo",
    { gitRef: "HEAD", cwd: scratch },
    /not a git repository/,
  );

  await run("git", ["init", "-q"], { cwd: scratch });
  await run("git", ["config", "user.email", "test@example.com"], { cwd: scratch });
  await run("git", ["config", "user.name", "Test"], { cwd: scratch });
  await run("git", ["add", "-A"], { cwd: scratch });
  await run("git", ["commit", "-qm", "initial"], { cwd: scratch });

  await expectError(
    "reports when nothing is staged",
    { gitRef: "staged", cwd: scratch },
    /Nothing is staged/,
  );

  await writeFile(join(scratch, "a.ts"), "export const a = 99;\n");
  const unstaged = await resolveSource({ gitRef: "unstaged", cwd: scratch });
  check("reads an unstaged diff", unstaged.content.includes("+export const a = 99;"));

  await run("git", ["add", "-A"], { cwd: scratch });
  const staged = await resolveSource({ gitRef: "staged", cwd: scratch });
  check("reads a staged diff", staged.content.includes("+export const a = 99;"));

  // --- secrets must never be transmitted ------------------------------------

  await writeFile(join(scratch, ".env"), "OPENROUTER_API_KEY=sk-or-v1-secret\n");
  await writeFile(join(scratch, "server.pem"), "-----BEGIN PRIVATE KEY-----\n");
  await writeFile(join(scratch, "credentials.json"), '{"token":"abc"}\n');

  await expectError(
    "refuses to read .env",
    { paths: [".env"], cwd: scratch },
    /credentials pattern/,
  );
  await expectError(
    "refuses to read a .pem",
    { paths: ["server.pem"], cwd: scratch },
    /credentials pattern/,
  );
  await expectError(
    "refuses to read credentials.json",
    { paths: ["credentials.json"], cwd: scratch },
    /credentials pattern/,
  );

  // --- confinement to cwd ---------------------------------------------------

  await expectError(
    "refuses a path escaping cwd",
    { paths: ["../../etc/passwd"], cwd: scratch },
    /outside the working directory/,
  );
  await expectError(
    "refuses an absolute path outside cwd",
    { paths: [process.platform === "win32" ? "C:\\Windows\\win.ini" : "/etc/passwd"], cwd: scratch },
    /outside the working directory|Cannot read/,
  );

  // --- git flag injection ---------------------------------------------------

  await expectError(
    "rejects a git flag posing as a ref",
    { gitRef: "--name-only", cwd: scratch },
    /is a git flag, not a revision/,
  );
  await expectError(
    "rejects --output as a ref",
    { gitRef: "--output=/tmp/pwned", cwd: scratch },
    /is a git flag, not a revision/,
  );

  // --- size guard -----------------------------------------------------------

  await expectError(
    "rejects oversized input",
    { diff: "x".repeat(600_000) },
    /over the .*KB limit/,
  );
} finally {
  await rm(scratch, { recursive: true, force: true });
}

console.log(failures === 0 ? "\nAll source tests passed." : `\n${failures} failure(s).`);
process.exit(failures === 0 ? 0 : 1);
