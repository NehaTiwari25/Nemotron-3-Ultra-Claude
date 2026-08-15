# Demo result

Reviewer: `nvidia/nemotron-3-ultra-550b-a55b:free`  ·  Elapsed: 57.9s  ·  Run: 2026-08-15T13:47:09.016Z

Target: `demo/src/cache.ts` — 5 planted defects, ground truth in `PLANTED_BUGS.md`.

---

## Second opinion (nvidia/nemotron-3-ultra-550b-a55b:free)

**4 findings.** The cache has critical async and batching bugs that cause incorrect results, plus major state-tracking and error-handling defects that corrupt long-running behavior.

### 1. [CRITICAL] getMany returns before batched fetches complete because forEach does not await async callbacks.

- **Where:** src/cache.ts:85-92 (getMany)
- **Category:** async-not-awaited
- **How it breaks:** Call getMany(['x','y']) with empty cache. The function returns an empty Map immediately; the upstream fetches run in the background but their results are never added to the returned Map.
- **Possible fix:** Replace forEach with a for...of loop and await each fetchChunk, or use Promise.all on mapped chunks and merge results before returning.

### 2. [CRITICAL] chunk slices with start + batchSize - 1, producing chunks one element smaller than batchSize.

- **Where:** src/cache.ts:73-78 (chunk)
- **Category:** off-by-one-batching
- **How it breaks:** batchSize=3, missing=['a','b','c','d','e','f']. Expected chunks: [['a','b','c'], ['d','e','f']]. Actual: [['a','b'], ['c','d'], ['e','f']] — three upstream calls instead of two, and the last chunk may be undersized.
- **Possible fix:** Change slice end to start + this.batchSize (remove the -1).

### 3. [MAJOR] size field is not decremented on eviction or invalidation, and is incremented on overwrite without removing the old entry, causing stats.size to diverge from entries.size.

- **Where:** src/cache.ts:55-68 (write), src/cache.ts:98-100 (invalidate)
- **Category:** cache-size-accounting
- **How it breaks:** maxEntries=2. write('a',1); write('b',2); write('c',3) — evicts 'a' but size becomes 3. invalidate('b') — entries.size=1 but size=3. stats reports {size:3, tracked:1}.
- **Possible fix:** Decrement size when deleting an entry (eviction, invalidate, or overwrite). In write, check if key exists before incrementing.

### 4. [MAJOR] fetchChunk catches all errors and returns an empty record, hiding upstream failures from callers.

- **Where:** src/cache.ts:94-97 (fetchChunk)
- **Category:** silent-error-swallow
- **How it breaks:** Upstream returns 500 or throws a network error. getMany returns cached entries plus empty results for missing keys, with no indication that the fetch failed. Caller cannot distinguish 'key not found' from 'upstream error'.
- **Possible fix:** Either propagate the error, or return a result object that distinguishes missing keys from fetch failures (e.g., { data, errors }).

---
_These are claims from a second model, not verified facts. Confirm each against the real code before acting on it._

---

## Score

| # | Planted bug | Found? |
|---|---|---|
| 1 | Off-by-one in `chunk()` | |
| 2 | `forEach` with async callback | |
| 3 | Swallowed upstream error | |
| 4 | `size` never decremented | |
| 5 | No request coalescing | |

False positives: 
