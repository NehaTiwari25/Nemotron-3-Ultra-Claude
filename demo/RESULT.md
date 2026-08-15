# Demo result

Reviewer: `nvidia/nemotron-3-ultra-550b-a55b:free`  ·  Elapsed: 147.1s  ·  Run: 2026-08-15T13:45:17.631Z

Target: `demo/src/cache.ts` — 5 planted defects, ground truth in `PLANTED_BUGS.md`.

---

## Second opinion (nvidia/nemotron-3-ultra-550b-a55b:free)

**5 findings.** The cache has critical async and batching bugs that break core functionality, a major state-tracking bug that defeats eviction, and a concurrency gap causing duplicate upstream calls.

### 1. [CRITICAL] getMany returns immediately without awaiting batched upstream fetches because forEach does not await async callbacks.

- **Where:** src/cache.ts:85-92
- **Category:** async-not-awaited
- **How it breaks:** Call getMany(['missing-id']) with empty cache. The function returns an empty Map instantly. The upstream fetch runs in the background but its results are never added to the returned Map. Caller sees no data for IDs that were successfully fetched.
- **Possible fix:** Replace forEach with a for...of loop and await each fetchChunk, or use Promise.all with map.

### 2. [CRITICAL] chunk slices with start + batchSize - 1, producing chunks one element smaller than batchSize.

- **Where:** src/cache.ts:73-78
- **Category:** off-by-one-batching
- **How it breaks:** batchSize=3, missing IDs ['a','b','c','d','e','f']. Expected chunks: [['a','b','c'], ['d','e','f']]. Actual chunks: [['a','b'], ['c','d'], ['e','f']]. Upstream receives 3 requests of 2 items instead of 2 requests of 3, increasing latency and request count.
- **Possible fix:** Change slice end to start + this.batchSize (remove -1).

### 3. [MAJOR] size counter is incremented on write but never decremented on eviction, invalidation, or expiry, causing eviction logic to trigger incorrectly and stats to diverge from reality.

- **Where:** src/cache.ts:55-68
- **Category:** stale-size-counter
- **How it breaks:** maxEntries=2. Write 'a', 'b', 'c'. Eviction deletes 'a' but size becomes 3. Stats show size=3, tracked=2. Next write evicts 'b' (oldest) but size grows to 4. Cache grows unbounded despite maxEntries limit.
- **Possible fix:** Decrement size in write when evicting, in invalidate, and in read when deleting expired entry. Or remove size and use entries.size directly.

### 4. [MAJOR] Concurrent getMany calls for the same missing IDs each trigger independent upstream fetches, wasting bandwidth and potentially overwhelming upstream.

- **Where:** src/cache.ts:79-94
- **Category:** missing-request-deduplication
- **How it breaks:** Two parallel calls to getMany(['x']) with cold cache. Both see 'x' as missing, both call upstream for ['x']. Upstream receives duplicate request. If upstream has side effects (e.g., rate limiting, mutations), this causes incorrect behavior.
- **Possible fix:** Track in-flight requests per ID (e.g., Map<string, Promise<T>>) and await existing promise instead of re-fetching.

### 5. [MINOR] fetchChunk catches all errors and returns empty object, giving callers no signal that upstream failed.

- **Where:** src/cache.ts:96-102
- **Category:** silent-error-swallow
- **How it breaks:** Upstream returns 500 for a chunk. fetchChunk returns {}. getMany succeeds but returns no data for those IDs. Caller cannot distinguish 'ID not found' from 'upstream error', making debugging and retry logic impossible.
- **Possible fix:** Propagate error or return a result object with error metadata. At minimum, log the error.

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
