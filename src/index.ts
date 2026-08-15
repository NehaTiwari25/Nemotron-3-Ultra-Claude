#!/usr/bin/env node
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { loadDotEnv } from "./env.js";
import { reviewCode } from "./review.js";
import { resolveSource } from "./sources.js";
import { activeModel } from "./openrouter.js";

// Must run before anything reads process.env.
loadDotEnv();

const server = new McpServer({
  name: "second-opinion",
  version: "0.1.0",
});

server.registerTool(
  "review_diff",
  {
    title: "Get a second opinion on a code change",
    description:
      "Sends code to a different model for adversarial review, and returns " +
      "behavioral defects with concrete failure scenarios. Use after writing " +
      "or modifying non-trivial code, before considering it done. A model " +
      "reviewing its own output misses its own blind spots; this routes the " +
      "review to a model with different priors.\n\n" +
      "Prefer `paths` or `git_ref` over `diff`: the server reads the code " +
      "itself, so the files never enter your context and only the findings " +
      "come back. Reviewing by path costs you a filename instead of a file.\n\n" +
      "Findings are claims to verify, not confirmed bugs.",
    inputSchema: {
      paths: z
        .array(z.string())
        .optional()
        .describe(
          "PREFERRED. Files for the server to read itself, e.g. " +
            '["src/cache.ts"]. Use this instead of pasting file contents — it ' +
            "keeps the code out of your own context entirely.",
        ),
      git_ref: z
        .string()
        .optional()
        .describe(
          'PREFERRED for changes. The server runs git diff itself. Accepts "staged", ' +
            '"unstaged", or any ref/range such as "HEAD~1" or "main".',
        ),
      cwd: z
        .string()
        .optional()
        .describe(
          "Directory to resolve `paths` and `git_ref` against. Defaults to the " +
            "server's working directory, so pass the repo root explicitly.",
        ),
      diff: z
        .string()
        .optional()
        .describe(
          "Literal diff or file text. Only use when the code is not on disk — " +
            "`paths` and `git_ref` are cheaper because they do not consume your context.",
        ),
      context: z
        .string()
        .optional()
        .describe(
          "Supporting code the diff depends on but does not include — type " +
            "definitions, called functions, schemas. Improves accuracy and " +
            "cuts false positives sharply.",
        ),
      focus: z
        .string()
        .optional()
        .describe(
          "Optional area to weight the review toward, e.g. 'concurrency', " +
            "'input validation', 'the retry logic'.",
        ),
      language: z
        .string()
        .optional()
        .describe("Primary language of the change, e.g. 'TypeScript', 'Python'."),
    },
  },
  async ({ paths, git_ref, cwd, diff, context, focus, language }) => {
    try {
      const source = await resolveSource({ paths, gitRef: git_ref, diff, cwd });
      const report = await reviewCode({
        content: source.content,
        description: source.description,
        context,
        focus,
        language,
      });
      return { content: [{ type: "text" as const, text: report }] };
    } catch (error) {
      return {
        isError: true,
        content: [
          {
            type: "text" as const,
            text: `Second opinion unavailable: ${(error as Error).message}`,
          },
        ],
      };
    }
  },
);

async function main(): Promise<void> {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  // stdout is the protocol channel — anything informational must go to stderr.
  console.error(`second-opinion ready (reviewer: ${activeModel()})`);
}

main().catch((error) => {
  console.error("second-opinion failed to start:", error);
  process.exit(1);
});
