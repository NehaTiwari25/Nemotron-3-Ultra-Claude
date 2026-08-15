# Ground truth

Five defects were deliberately planted in `demo/src/cache.ts`, spanning five
different categories the reviewer prompt targets. **This file is never sent to
the reviewer.** Score the demo by how many it independently finds.

The code compiles and type-checks cleanly, and no linter will flag any of it.
Bugs 1, 4, and 5 are invisible to sequential single-item tests — they need a
chunk boundary, a long-running process, or real concurrency to appear. That is
the point: every one of these survives a skim and most survive a test suite.

---

### 1. Off-by-one in `chunk()` — silent data loss
**Category:** boundary error · **Severity:** critical

```ts
chunks.push(ids.slice(start, start + this.batchSize - 1));
```

`slice` is already end-exclusive, so the `- 1` drops the last id of every chunk.

**Failure:** 100 ids at `batchSize: 50` produces chunks of 49 and 49. Two ids are
never requested upstream and are silently absent from the result. The caller
cannot distinguish this from "those records don't exist."

---

### 2. `forEach` with an async callback — returns before any fetch completes
**Category:** async correctness · **Severity:** critical

```ts
chunks.forEach(async (chunk) => {
  const fetched = await this.fetchChunk(chunk);
  ...
});
return result;
```

`Array.prototype.forEach` ignores the returned promises. `getMany` returns
immediately, before a single upstream call resolves.

**Failure:** any cache miss returns an empty (or partial) map. The writes land
later, mutating a Map the caller already received — so the same call appears to
"work" on a second run, which is exactly what makes this survive testing.
Needs `await Promise.all(chunks.map(...))`.

---

### 3. Swallowed upstream error in `fetchChunk()`
**Category:** error handling · **Severity:** major

```ts
} catch (error) {
  return {};
}
```

An upstream outage becomes indistinguishable from "no such records."

**Failure:** the API 500s. Nothing is cached, no error surfaces, and the caller
retries every request forever against a failing upstream. No log line, no
metric, no signal.

---

### 4. `size` is incremented but never decremented — the cache destroys itself
**Category:** state consistency · **Severity:** major

`write()` does `this.size++` unconditionally. The expiry delete in `read()`,
the eviction delete in `write()`, and `invalidate()` all remove entries without
decrementing.

**Failure:** `size` climbs monotonically regardless of real occupancy. Once it
passes `maxEntries` (1000 by default) it can never come back down, so *every
subsequent write evicts an entry*. The cache silently degrades to holding
roughly one useful entry, and hit rate collapses to near zero in long-running
processes. The `stats` getter exposing both `size` and `tracked` is the tell —
they diverge immediately.

This is the "two changes that are individually correct but leave state
inconsistent" class: the increment is right, each delete is right, the pairing
is missing.

---

### 5. No request coalescing — concurrent misses stampede upstream
**Category:** concurrency · **Severity:** major

There is no in-flight request map. Nothing deduplicates concurrent misses for
the same id.

**Failure:** a cold cache serving 200 concurrent requests for the same id fires
200 upstream calls instead of 1. Classic thundering herd — the failure mode
appears only under concurrency, so it never shows up in sequential tests, and it
hits hardest exactly when the system is already under load.

---

## Scoring

| Found | Read as |
|---|---|
| 4–5 | Genuinely useful. Worth running on real changes. |
| 2–3 | Useful but needs the diff plus context to do better. |
| 0–1 | The prompt needs work, or the model isn't right for this. |

Record false positives too. A reviewer that finds all five plus six imaginary
ones is not a good reviewer — noise is what makes people stop reading output.
