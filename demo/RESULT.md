# Demo result

Reviewer: `nvidia/nemotron-3-ultra-550b-a55b:free`  ·  Elapsed: 85.7s  ·  Run: 2026-08-15T13:28:31.829Z

Target: `demo/src/cache.ts` — 5 planted defects, ground truth in `PLANTED_BUGS.md`.

---

## Second opinion (nvidia/nemotron-3-ultra-550b-a55b:free)

**6 findings.** The cache has a critical async bug that breaks its core contract, plus major defects in batching, size tracking, concurrency, and error handling that would surface under load or failure conditions.

### 1. [CRITICAL] getMany returns before upstream fetches complete because forEach does not await async callbacks.

- **Where:** src/cache.ts:85-92
- **Category:** async-not-awaited
- **How it breaks:** Call getMany(['a', 'b']) with both keys missing. The function returns an empty Map immediately. The two upstream requests fire in background and eventually write to cache, but the caller never sees those values.
- **Possible fix:** Replace forEach with a for...of loop and await each fetchChunk, or use Promise.all with map.

### 2. [MAJOR] chunk produces batches of size batchSize-1 due to incorrect slice end index.

- **Where:** src/cache.ts:73-77
- **Category:** off-by-one-batching
- **How it breaks:** With batchSize=50 and 50 missing IDs, chunk returns two batches: one of 49 and one of 1, causing two upstream requests instead of one.
- **Possible fix:** Change slice end to start + this.batchSize (remove the -1).

### 3. [MAJOR] size field is incremented on write but never decremented on deletion, causing eviction and stats to diverge from reality.

- **Where:** src/cache.ts:38-52
- **Category:** size-tracking-corruption
- **How it breaks:** Insert 1000 entries (size=1000). Let all expire. Read all 1000 — each read deletes the entry but size stays 1000. Insert one new entry: size becomes 1001, eviction triggers and deletes one entry, but size becomes 1002. stats returns {size: 1002, tracked: 1}.
- **Possible fix:** Decrement size in read (on expiry), invalidate, and the eviction block. Or remove size entirely and use entries.size.

### 4. [MAJOR] Concurrent getMany calls for the same missing keys each fire independent upstream requests.

- **Where:** src/cache.ts:79-92
- **Category:** missing-inflight-deduplication
- **How it breaks:** Two callers simultaneously invoke getMany(['x']) where 'x' is not cached. Both see 'x' as missing, both call upstream for ['x']. Upstream receives two requests for the same ID.
- **Possible fix:** Track in-flight promises per key (or per chunk) and await existing promise if a request for the same IDs is already pending.

### 5. [MAJOR] fetchChunk catches all errors and returns empty object, making upstream failures indistinguishable from empty results.

- **Where:** src/cache.ts:95-101
- **Category:** swallowed-upstream-errors
- **How it breaks:** Upstream throws a 500 error. fetchChunk returns {}. getMany returns a result Map missing the requested keys, with no error surfaced. Caller cannot tell if keys don't exist or if upstream failed.
- **Possible fix:** Either propagate the error, or return a result structure that distinguishes 'not found' from 'error'.

### 6. [MINOR] Eviction removes only one entry when size exceeds maxEntries, potentially leaving cache over capacity.

- **Where:** src/cache.ts:43-49
- **Category:** inefficient-eviction
- **How it breaks:** If size somehow reaches maxEntries + 100 (e.g., due to size tracking bug), a single write triggers eviction of only one entry, leaving cache at maxEntries + 99.
- **Possible fix:** Loop eviction until size < maxEntries, or compute how many to remove and delete in bulk.

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
