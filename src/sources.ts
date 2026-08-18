import { execFile } from "node:child_process";
import { readFile, stat } from "node:fs/promises";
import { promisify } from "node:util";
import { isAbsolute, relative, resolve } from "node:path";

const run = promisify(execFile);

/**
 * Where the code under review comes from.
 *
 * The point of the path and git sources is context economy. When the caller
 * passes literal diff text, that text must already be in the calling agent's
 * context — so the tool saves reasoning tokens but not reading tokens. Letting
 * the server fetch the code itself means the agent spends a path instead of a
 * file, which is the difference between a few tokens and a few thousand.
 */
export interface SourceSpec {
  diff?: string;
  paths?: string[];
  gitRef?: string;
  cwd?: string;
}

export interface ResolvedSource {
  /** The text handed to the reviewer. */
  content: string;
  /** Human-readable description of what was reviewed, for the report header. */
  description: string;
  bytes: number;
}

/**
 * Beyond this the review gets slow and the model's attention thins out. Better
 * to fail with an instruction to narrow the scope than to silently truncate
 * and return a review of half the change.
 */
const MAX_BYTES = 500_000;

export class SourceError extends Error {}

/**
 * Files whose contents must never be shipped to a third-party API.
 *
 * The reviewer runs with the user's own permissions, so this is not a
 * privilege boundary — the calling agent could read any of these itself. The
 * risk is different and worse: whatever this server reads gets *transmitted*,
 * and the default endpoint retains free-tier data. A mistaken path, or a
 * prompt-injected one from a hostile repository, would exfiltrate credentials
 * rather than merely expose them locally.
 */
const SECRET_PATTERNS: RegExp[] = [
  /(^|[/\\])\.env(\.|$)/i,
  /(^|[/\\])\.npmrc$/i,
  /(^|[/\\])\.netrc$/i,
  /(^|[/\\])\.git-credentials$/i,
  /(^|[/\\])id_(rsa|dsa|ecdsa|ed25519)$/i,
  /\.(pem|key|pfx|p12|keystore|jks)$/i,
  /(^|[/\\])(credentials|secrets?)\.(json|ya?ml|toml|ini)$/i,
  /(^|[/\\])\.aws[/\\]/i,
  /(^|[/\\])\.ssh[/\\]/i,
];

function assertNotSecret(label: string, full: string): void {
  const target = SECRET_PATTERNS.some((p) => p.test(full) || p.test(label));
  if (target) {
    throw new SourceError(
      `Refusing to read "${label}": it matches a credentials pattern, and this ` +
        "server transmits whatever it reads to an external API. Review the code " +
        "that uses the secret, not the secret itself.",
    );
  }
}

/**
 * Reviewing files outside the working directory is legitimate but rare, and it
 * is also what a path-confusion mistake looks like. Require it to be opted into.
 */
function assertWithinCwd(label: string, full: string, cwd: string): void {
  if (process.env.SECOND_OPINION_ALLOW_OUTSIDE_CWD === "1") return;

  const rel = relative(resolve(cwd), full);
  const escapes = rel.startsWith("..") || isAbsolute(rel);
  if (escapes) {
    throw new SourceError(
      `"${label}" resolves outside the working directory (${cwd}). Pass \`cwd\` ` +
        "pointing at the right project, or set SECOND_OPINION_ALLOW_OUTSIDE_CWD=1 " +
        "if you genuinely mean to send that file to an external API.",
    );
  }
}

function assertSize(content: string, what: string): void {
  const bytes = Buffer.byteLength(content, "utf8");
  if (bytes > MAX_BYTES) {
    throw new SourceError(
      `${what} is ${(bytes / 1024).toFixed(0)}KB, over the ${MAX_BYTES / 1024}KB limit. ` +
        "Review a narrower set of files, or a single commit range.",
    );
  }
}

async function isGitRepo(cwd: string): Promise<boolean> {
  try {
    await run("git", ["rev-parse", "--git-dir"], { cwd });
    return true;
  } catch {
    return false;
  }
}

/**
 * `git diff` with no ref shows unstaged work, which is usually what someone
 * means by "review my changes". "staged" and "HEAD" are common enough to be
 * worth special-casing so callers don't have to know git's flag surface.
 */
function gitDiffArgs(ref: string): string[] {
  if (ref === "staged" || ref === "cached") return ["diff", "--cached"];
  if (ref === "unstaged" || ref === "working") return ["diff"];

  // A ref beginning with "-" is read by git as a flag, not a revision. That
  // silently changes what gets reviewed (`--name-only` yields filenames, not
  // code) and some flags have side effects (`--output=` writes to disk).
  if (ref.startsWith("-")) {
    throw new SourceError(
      `"${ref}" is a git flag, not a revision. Pass a ref such as "HEAD~1", ` +
        '"main", or one of "staged" / "unstaged".',
    );
  }

  // The trailing "--" stops git treating a ref that collides with a filename
  // as a path specifier.
  return ["diff", ref, "--"];
}

async function fromGit(ref: string, cwd: string): Promise<ResolvedSource> {
  if (!(await isGitRepo(cwd))) {
    throw new SourceError(
      `${cwd} is not a git repository. Pass \`cwd\` pointing at the repo root, ` +
        "or use `paths` instead.",
    );
  }

  let stdout: string;
  try {
    ({ stdout } = await run("git", gitDiffArgs(ref), {
      cwd,
      maxBuffer: MAX_BYTES * 2,
    }));
  } catch (cause) {
    // Keep the whole message: git's hint lines are usually the actionable part.
    throw new SourceError(`git diff failed for ref "${ref}": ${(cause as Error).message}`);
  }

  if (!stdout.trim()) {
    throw new SourceError(
      `No changes found for "${ref}". ` +
        (ref === "staged"
          ? "Nothing is staged."
          : "The working tree may be clean, or the ref may already be merged."),
    );
  }

  assertSize(stdout, `The diff for "${ref}"`);

  return {
    content: stdout,
    description: `git diff ${ref} in ${cwd}`,
    bytes: Buffer.byteLength(stdout, "utf8"),
  };
}

async function fromPaths(paths: string[], cwd: string): Promise<ResolvedSource> {
  const sections: string[] = [];
  // Deduplicated on the RESOLVED path, so "a.ts" and "/abs/a.ts" collapse, but
  // remembered by the caller's own spelling: the description is echoed in the
  // report header, and absolute machine paths there help nobody.
  const seen = new Set<string>();
  const kept: string[] = [];

  for (const entry of paths) {
    const full = isAbsolute(entry) ? entry : resolve(cwd, entry);

    if (seen.has(full)) continue;
    seen.add(full);
    kept.push(entry);

    // Guard before touching the filesystem: a path that should be refused must
    // be refused whether or not it happens to exist, and a rejected path should
    // not be probed for existence either.
    assertNotSecret(entry, full);
    assertWithinCwd(entry, full, cwd);

    let info;
    try {
      info = await stat(full);
    } catch {
      throw new SourceError(`Cannot read "${entry}" (resolved to ${full}).`);
    }

    if (info.isDirectory()) {
      throw new SourceError(
        `"${entry}" is a directory. List the files explicitly — reviewing a whole ` +
          "tree at once produces shallow results.",
      );
    }

    const text = await readFile(full, "utf8");
    const label = isAbsolute(entry) ? relative(cwd, full) || full : entry;
    sections.push(`// ===== File: ${label} =====\n\n${text}`);
  }

  const content = sections.join("\n\n");
  assertSize(content, `${kept.length} file(s)`);

  return {
    content,
    description:
      kept.length === 1 ? kept[0] : `${kept.length} files: ${kept.join(", ")}`,
    bytes: Buffer.byteLength(content, "utf8"),
  };
}

export async function resolveSource(spec: SourceSpec): Promise<ResolvedSource> {
  const cwd = spec.cwd ?? process.cwd();

  const provided = [
    spec.diff ? "diff" : null,
    spec.paths?.length ? "paths" : null,
    spec.gitRef ? "git_ref" : null,
  ].filter(Boolean);

  if (provided.length === 0) {
    throw new SourceError(
      "Provide one source: `paths` (files to read), `git_ref` (e.g. \"staged\", " +
        '"HEAD~1", "main"), or `diff` (literal diff text).',
    );
  }

  if (provided.length > 1) {
    throw new SourceError(
      `Provide exactly one source, got ${provided.length}: ${provided.join(", ")}.`,
    );
  }

  if (spec.gitRef) return fromGit(spec.gitRef, cwd);
  if (spec.paths?.length) return fromPaths(spec.paths, cwd);

  const diff = spec.diff!;
  assertSize(diff, "The supplied diff");
  return {
    content: diff,
    description: "supplied diff",
    bytes: Buffer.byteLength(diff, "utf8"),
  };
}
