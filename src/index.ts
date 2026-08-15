#!/usr/bin/env node
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { loadDotEnv } from "./env.js";
import { reviewDiff } from "./review.js";
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
      "Sends a diff to a different model for adversarial review, and returns " +
      "behavioral defects with concrete failure scenarios. Use this after " +
      "writing or modifying non-trivial code, before considering it done. " +
      "A model reviewing its own output misses its own blind spots; this " +
      "routes the review to a model with different priors. Findings are " +
      "claims to verify, not confirmed bugs.",
    inputSchema: {
      diff: z
        .string()
        .min(1)
        .describe(
          "The change to review. A unified diff is ideal; whole files are " +
            "acceptable for new code.",
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
  async ({ diff, context, focus, language }) => {
    try {
      const report = await reviewDiff({ diff, context, focus, language });
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
