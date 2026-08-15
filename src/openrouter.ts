/**
 * Minimal OpenRouter client.
 *
 * Deliberately dependency-free (uses global fetch) so the server stays easy to
 * audit and install. Handles the two things that actually bite in practice:
 * the free-tier request-per-minute cap, and quota exhaustion masquerading as a
 * generic HTTP error.
 */

const API_URL = "https://openrouter.ai/api/v1/chat/completions";

/** Free variants are capped at 20 req/min, so stay just under that. */
const MIN_REQUEST_INTERVAL_MS = 3_100;

export interface ChatMessage {
  role: "system" | "user" | "assistant";
  content: string;
}

export interface CompletionOptions {
  messages: ChatMessage[];
  /** Overrides SECOND_OPINION_MODEL for a single call. */
  model?: string;
  temperature?: number;
  maxTokens?: number;
}

export class OpenRouterError extends Error {
  constructor(
    message: string,
    readonly status?: number,
    readonly retryable = false,
  ) {
    super(message);
    this.name = "OpenRouterError";
  }
}

let lastRequestAt = 0;

/**
 * Serialises waiters through a promise chain.
 *
 * Reading and updating `lastRequestAt` inline is not enough: MCP clients may
 * invoke tools in parallel, and concurrent callers would each observe the same
 * `lastRequestAt`, sleep the same interval, and then fire together — a burst
 * rather than a throttle. Chaining makes each caller wait for the previous one
 * to claim its slot before computing its own.
 */
let queue: Promise<void> = Promise.resolve();

function throttle(): Promise<void> {
  const slot = queue.then(async () => {
    const wait = MIN_REQUEST_INTERVAL_MS - (Date.now() - lastRequestAt);
    if (wait > 0) await new Promise((r) => setTimeout(r, wait));
    lastRequestAt = Date.now();
  });

  // Swallow rejections on the chain itself so one failure cannot wedge the
  // queue for every subsequent caller; `slot` still surfaces them to its owner.
  queue = slot.catch(() => undefined);

  return slot;
}

function defaultModel(): string {
  return (
    process.env.SECOND_OPINION_MODEL ??
    "nvidia/nemotron-3-ultra-550b-a55b:free"
  );
}

function apiKey(): string {
  const key = process.env.OPENROUTER_API_KEY;
  if (!key) {
    throw new OpenRouterError(
      "OPENROUTER_API_KEY is not set. Get a key at https://openrouter.ai/keys " +
        "and add it to your MCP server config's `env` block.",
    );
  }
  return key;
}

/**
 * Turns an error response into something a human can act on. OpenRouter
 * returns 429 for both "too fast" and "daily quota gone", which need very
 * different responses from the caller, so we split them here.
 */
function describeFailure(status: number, body: string): OpenRouterError {
  const lower = body.toLowerCase();

  if (status === 401) {
    return new OpenRouterError(
      "OpenRouter rejected the API key (401). Check OPENROUTER_API_KEY.",
      status,
    );
  }

  if (status === 429) {
    const quotaExhausted =
      lower.includes("daily") || lower.includes("quota") || lower.includes("credit");
    if (quotaExhausted) {
      return new OpenRouterError(
        "Daily free-tier quota exhausted. Accounts under $10 lifetime credit get " +
          "50 free requests/day; adding $10 once raises it to 1000/day permanently. " +
          "Alternatively set SECOND_OPINION_MODEL to a paid model.",
        status,
      );
    }
    return new OpenRouterError(
      "Rate limited (20 req/min on free variants). Retrying.",
      status,
      true,
    );
  }

  if (status >= 500) {
    return new OpenRouterError(
      `OpenRouter upstream error (${status}). Retrying.`,
      status,
      true,
    );
  }

  return new OpenRouterError(
    `OpenRouter request failed (${status}): ${body.slice(0, 400)}`,
    status,
  );
}

export async function complete(options: CompletionOptions): Promise<string> {
  const model = options.model ?? defaultModel();
  const key = apiKey();

  const maxAttempts = 4;
  let lastError: OpenRouterError | undefined;

  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    await throttle();

    let response: Response;
    try {
      response = await fetch(API_URL, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${key}`,
          "Content-Type": "application/json",
          // Optional attribution headers OpenRouter uses for its leaderboards.
          "HTTP-Referer": "https://github.com/second-opinion-mcp",
          "X-Title": "Second Opinion MCP",
        },
        body: JSON.stringify({
          model,
          messages: options.messages,
          temperature: options.temperature ?? 0.2,
          max_tokens: options.maxTokens ?? 4096,
        }),
      });
    } catch (cause) {
      lastError = new OpenRouterError(
        `Network error contacting OpenRouter: ${(cause as Error).message}`,
        undefined,
        true,
      );
      await backoff(attempt);
      continue;
    }

    if (!response.ok) {
      const body = await response.text().catch(() => "");
      const error = describeFailure(response.status, body);
      if (!error.retryable || attempt === maxAttempts) throw error;
      lastError = error;
      await backoff(attempt);
      continue;
    }

    const payload = (await response.json()) as {
      choices?: { message?: { content?: string } }[];
    };
    const content = payload.choices?.[0]?.message?.content;

    if (!content) {
      throw new OpenRouterError(
        `Model "${model}" returned an empty response. It may be unavailable — ` +
          "check https://openrouter.ai/models for current status.",
      );
    }

    return content;
  }

  throw lastError ?? new OpenRouterError("Request failed after retries.");
}

function backoff(attempt: number): Promise<void> {
  const ms = Math.min(2 ** attempt * 1000, 15_000);
  return new Promise((r) => setTimeout(r, ms));
}

export function activeModel(): string {
  return defaultModel();
}
