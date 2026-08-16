# Second Opinion

**Your AI reviews its own code. That's the bug.**

An MCP server that routes code review to a *different* model than the one that wrote the code — pairing Claude Code with NVIDIA's Nemotron 3 Ultra.

---

## The problem

When a coding agent reviews its own output, it is confident about precisely the things it got wrong. The same weights that produced the hallucinated method signature will read that signature back and see nothing unusual. Self-review has a blind spot exactly the shape of the model's own priors.

A second model with different training has different blind spots. The overlap is where real bugs hide — and the disagreement between them is signal you can act on.

Second Opinion gives your agent a `review_diff` tool. It sends the change to another model, gets back behavioral defects with concrete failure scenarios, and hands them to your agent to verify.

## Install

```bash
npm install -g second-opinion-mcp
```

Then add it to your MCP client. For **Claude Code**, in `.mcp.json` at your project root (or `~/.claude.json` for all projects):

```json
{
  "mcpServers": {
    "second-opinion": {
      "command": "npx",
      "args": ["-y", "second-opinion-mcp"],
      "env": {
        "OPENROUTER_API_KEY": "sk-or-v1-..."
      }
    }
  }
}
```

Get a key at [openrouter.ai/keys](https://openrouter.ai/keys).

## Cost

The default reviewer is **NVIDIA Nemotron 3 Ultra** (550B params, ~55B active) on OpenRouter's free tier. Reviews cost nothing.

The free tier is capped by request count, not tokens:

| Account | Requests/day |
|---|---|
| Under $10 lifetime credit | 50 |
| $10+ credit added once | 1,000 |

Both are capped at 20 requests/minute; the server throttles itself to stay under it.

To use a different reviewer, set `SECOND_OPINION_MODEL` to any [OpenRouter model ID](https://openrouter.ai/models):

```json
"env": {
  "OPENROUTER_API_KEY": "sk-or-v1-...",
  "SECOND_OPINION_MODEL": "deepseek/deepseek-r1"
}
```

## Running fully offline

Any OpenAI-compatible endpoint works, so a local model needs no code change — only a URL. With [Ollama](https://ollama.com):

```bash
ollama pull nemotron-3-nano:4b
```

```json
"env": {
  "SECOND_OPINION_BASE_URL": "http://localhost:11434/v1",
  "SECOND_OPINION_MODEL": "nemotron-3-nano:4b"
}
```

No API key is needed for a local endpoint, and the rate-limit throttle is skipped. Nothing leaves your machine — which makes this the option to use for client work or any code you cannot send to a third party.

**Be realistic about the tradeoff.** On the bundled 5-bug demo, `nemotron-3-ultra` found 5 of 5; `nemotron-3-nano:4b` found 1 of 5 (though its explanation of that one was more accurate than the larger model's). Local is the right answer for privacy and for unlimited runs, not for review quality.

## Usage

Once configured, just ask:

> Review that change with a second opinion.

Or let your agent call it on its own — the tool description tells it to reach for this after non-trivial edits.

Passing `context` (type definitions, called functions, schemas the diff depends on but doesn't contain) meaningfully cuts false positives. The reviewer can't reason about code it can't see.

## It costs your agent almost no context

The tool takes **file paths or a git ref**, and the server reads the code itself. Your agent spends a filename; the file never enters its context window. Only the findings come back.

```jsonc
{ "paths": ["src/cache.ts"] }          // server reads the file
{ "git_ref": "staged" }                // server runs git diff --cached
{ "git_ref": "HEAD~1", "cwd": "..." }  // any ref or range
{ "diff": "..." }                      // literal text, when it isn't on disk
```

For a 500-line file the difference is roughly:

| Approach | Tokens in your agent's context |
|---|---|
| Agent reads and reviews it itself | ~7,000 (file + reasoning) |
| Pasting the file into `diff` | ~7,500 (file + findings) |
| **Reviewing by `paths`** | **~500 (findings only)** |

The reasoning happens inside a model with a 1M context window that costs nothing, instead of inside the context you're paying for.

Exactly one source may be given per call.

### What it refuses to read

Because this server **transmits whatever it reads** to an external API, reading is constrained:

- **Credentials are refused outright** — `.env*`, `.pem`/`.key`/`.p12`, `id_rsa`, `.npmrc`, `.netrc`, `.git-credentials`, `credentials.json`, anything under `.ssh/` or `.aws/`.
- **Paths must stay inside `cwd`.** Escaping it needs `SECOND_OPINION_ALLOW_OUTSIDE_CWD=1`.
- **`git_ref` must be a revision, not a flag.** `--name-only` would silently review filenames instead of code; `--output=` writes to disk.

This isn't a privilege boundary — your agent could read those files itself. The difference is that this server *sends* them somewhere. A mistaken path, or a prompt-injected one from a hostile repository, would exfiltrate a key rather than merely print it.

## What it looks for

The prompt targets the failure modes characteristic of *generated* code:

- APIs, methods, or parameters that don't exist, or exist with a different signature
- Boundary and off-by-one errors
- Errors caught and silently swallowed
- Races, unawaited promises, state mutated out of order
- Null/undefined paths the happy path never exercises
- Changes that are individually correct but cancel out or leave state inconsistent
- Injection, path traversal, unvalidated input, leaked secrets

Every finding must carry a concrete failure scenario — specific inputs, specific wrong result. Findings that can't be grounded that way get dropped before they reach you. This is what keeps the output from degrading into style opinions.

Style, naming, and formatting are explicitly out of scope. You have a linter.

## Honest limitations

- **Findings are claims, not facts.** A second model hallucinates too. Everything it reports needs verifying against the real code. The tool output says so, and your agent should treat it that way.
- **No finding ≠ correct.** It means this reviewer found nothing. That's weaker than it sounds.
- **Two models share some blind spots.** Different training helps; it isn't independence. Overlapping failure modes stay invisible.
- **Your code leaves your machine.** OpenRouter's free NVIDIA endpoint carries a notice that session data is collected for product improvement. Don't send proprietary or client code through the free tier — use a paid endpoint with a retention policy you've read, or don't send it at all.

## Roadmap

- `best_of_n` — generate N candidates, select by running tests rather than by asking a model which looks better
- `bulk_generate` — delegate mechanical work (test stubs, docstrings, format conversion) to the cheap model
- Local reviewer via a fine-tuned Nemotron Nano, no API round trip

## Development

```bash
npm install
npm test        # 29 tests: source resolution, throttling, MCP protocol. No API calls.
```

Review something from the command line:

```bash
npm run review -- src/openrouter.ts
npm run review -- --git staged
npm run review -- --git HEAD~1 --focus "error handling"
```

Run the scored demo (uses one API request):

```bash
node demo/run-demo.mjs
```

## Using this?

Open an [issue](https://github.com/NehaTiwari25/Nemotron-3-Ultra-Claude/issues) or a discussion and say hi — I'd like to know what it's catching for you, and what it's missing.

Findings it got wrong are especially useful. The prompt is the product here, and false positives are the thing most likely to make people stop reading its output.

## License

MIT
