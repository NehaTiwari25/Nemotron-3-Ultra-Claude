import { complete, activeModel } from "./openrouter.js";

export interface Finding {
  severity: "critical" | "major" | "minor";
  category: string;
  location: string;
  claim: string;
  failure_scenario: string;
  suggested_fix?: string;
}

export interface ReviewRequest {
  /** The code under review, already resolved from whatever source. */
  content: string;
  /** What was reviewed, echoed in the report header. */
  description: string;
  context?: string;
  focus?: string;
  language?: string;
}

/**
 * The review prompt is the actual product here.
 *
 * Two design choices carry most of the value:
 *
 * 1. It targets the failure modes specific to *generated* code. A model
 *    reviewing its own output is confident about precisely the things it got
 *    wrong; a different model has different priors, which is the entire reason
 *    this server exists. Pointing it at hallucinated APIs, boundary conditions
 *    and silently-swallowed errors is where that difference actually cashes out.
 *
 * 2. Every finding must carry a concrete failure scenario — specific inputs or
 *    state leading to a specific wrong result. This is the discipline that
 *    separates real bugs from stylistic opinion, and it makes the reviewer
 *    discard its own weak findings before they reach you.
 */
const SYSTEM_PROMPT = `You are a senior engineer performing an adversarial code review.

The code under review was likely written by an AI coding assistant. That matters, because AI-generated code fails in characteristic ways:

- Calls to APIs, methods, or parameters that do not exist, or exist with a different signature
- Boundary and off-by-one errors at the edges of loops, slices, and ranges
- Errors caught and silently swallowed, hiding real failures
- Async code with races, unawaited promises, or state mutated out of order
- Null/undefined paths that the happy-path case never exercises
- Two changes that are individually correct but cancel each other out, or leave state inconsistent
- Confident-looking code that handles the example input and nothing else
- Security: injection, path traversal, unvalidated input, leaked secrets

RULES:

1. Report only defects that change behavior. No style, naming, or formatting opinions. No praise.
2. Every finding MUST include a concrete failure scenario: specific inputs or state, and the specific wrong output, crash, or corrupted state that results. If you cannot construct one, the finding is not real — drop it.
3. Do not speculate about code you cannot see. If a symbol is defined outside the diff, either reason from its usage or say nothing about it.
4. Prefer few high-confidence findings over many weak ones. Reporting nothing is a valid and useful result.
5. Rank findings most severe first.

Respond with ONLY a JSON object, no markdown fence, in exactly this shape:

{
  "findings": [
    {
      "severity": "critical" | "major" | "minor",
      "category": "short-kebab-case-slug",
      "location": "file path and line or function name",
      "claim": "one sentence stating the defect",
      "failure_scenario": "specific inputs/state -> specific wrong result",
      "suggested_fix": "optional, one or two sentences"
    }
  ],
  "summary": "one sentence on the overall state of the change"
}`;

function buildUserPrompt(request: ReviewRequest): string {
  const parts: string[] = [];

  if (request.language) {
    parts.push(`Language: ${request.language}`);
  }
  if (request.focus) {
    parts.push(`The requester wants particular attention paid to: ${request.focus}`);
  }
  if (request.context) {
    parts.push(`Surrounding context (not part of the change):\n\n${request.context}`);
  }

  parts.push(`Review this change:\n\n${request.content}`);
  return parts.join("\n\n---\n\n");
}

/**
 * Models wrap JSON in prose or fences often enough that a strict parse is not
 * worth the failed reviews. Fall back to extracting the outermost object.
 */
function parseResponse(raw: string): { findings: Finding[]; summary: string } {
  const attempt = (text: string) => {
    const parsed = JSON.parse(text) as { findings?: Finding[]; summary?: string };
    return {
      findings: Array.isArray(parsed.findings) ? parsed.findings : [],
      summary: parsed.summary ?? "",
    };
  };

  try {
    return attempt(raw.trim());
  } catch {
    // Strip a markdown fence if present, else grab the outermost braces.
    const fenced = raw.match(/```(?:json)?\s*([\s\S]*?)```/);
    if (fenced) {
      try {
        return attempt(fenced[1].trim());
      } catch {
        /* fall through */
      }
    }

    const start = raw.indexOf("{");
    const end = raw.lastIndexOf("}");
    if (start !== -1 && end > start) {
      try {
        return attempt(raw.slice(start, end + 1));
      } catch {
        /* fall through */
      }
    }

    throw new Error(
      `Reviewer returned unparseable output. First 500 chars:\n${raw.slice(0, 500)}`,
    );
  }
}

const SEVERITY_ORDER = { critical: 0, major: 1, minor: 2 } as const;

export async function reviewCode(request: ReviewRequest): Promise<string> {
  const raw = await complete({
    messages: [
      { role: "system", content: SYSTEM_PROMPT },
      { role: "user", content: buildUserPrompt(request) },
    ],
    temperature: 0.1,
    maxTokens: 6000,
  });

  const { findings, summary } = parseResponse(raw);
  return formatFindings(findings, summary, request.description);
}

function formatFindings(
  findings: Finding[],
  summary: string,
  description: string,
): string {
  const model = activeModel();
  const header = `## Second opinion — ${description}\n_Reviewer: ${model}_`;

  if (findings.length === 0) {
    return [
      header,
      "",
      "**No behavioral defects found.**",
      summary ? `\n${summary}` : "",
      "",
      "_Note: a clean result means this reviewer found nothing, not that the change is correct._",
    ].join("\n");
  }

  const sorted = [...findings].sort(
    (a, b) => (SEVERITY_ORDER[a.severity] ?? 3) - (SEVERITY_ORDER[b.severity] ?? 3),
  );

  const lines: string[] = [
    header,
    "",
    `**${sorted.length} finding${sorted.length === 1 ? "" : "s"}.** ${summary}`,
    "",
  ];

  for (const [index, finding] of sorted.entries()) {
    const badge = finding.severity?.toUpperCase() ?? "UNKNOWN";
    lines.push(`### ${index + 1}. [${badge}] ${finding.claim}`);
    lines.push("");
    lines.push(`- **Where:** ${finding.location}`);
    lines.push(`- **Category:** ${finding.category}`);
    lines.push(`- **How it breaks:** ${finding.failure_scenario}`);
    if (finding.suggested_fix) {
      lines.push(`- **Possible fix:** ${finding.suggested_fix}`);
    }
    lines.push("");
  }

  lines.push("---");
  lines.push(
    "_These are claims from a second model, not verified facts. Confirm each " +
      "against the real code before acting on it._",
  );

  return lines.join("\n");
}
