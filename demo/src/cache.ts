/**
 * A TTL cache in front of a batched upstream API.
 *
 * This is the code under review in the demo. It is written to look like
 * competent, plausible AI output: clean structure, sensible names, reasonable
 * comments, no obvious smells. The defects are the kind that survive a
 * skim and a happy-path test.
 *
 * Ground truth lives in demo/PLANTED_BUGS.md and is never sent to the reviewer.
 */

export interface CacheEntry<T> {
  value: T;
  expiresAt: number;
}

export interface FetcherOptions {
  /** Max items per upstream request. */
  batchSize?: number;
  /** Entry lifetime in ms. */
  ttlMs?: number;
  /** Max entries retained before eviction kicks in. */
  maxEntries?: number;
}

type Upstream<T> = (ids: string[]) => Promise<Record<string, T>>;

export class BatchedCache<T> {
  private entries = new Map<string, CacheEntry<T>>();
  private size = 0;

  private readonly batchSize: number;
  private readonly ttlMs: number;
  private readonly maxEntries: number;

  constructor(
    private readonly upstream: Upstream<T>,
    options: FetcherOptions = {},
  ) {
    this.batchSize = options.batchSize ?? 50;
    this.ttlMs = options.ttlMs ?? 60_000;
    this.maxEntries = options.maxEntries ?? 1000;
  }

  /** Returns a cached value, or undefined if absent or stale. */
  private read(id: string): T | undefined {
    const entry = this.entries.get(id);
    if (!entry) return undefined;

    if (entry.expiresAt < Date.now()) {
      this.entries.delete(id);
      return undefined;
    }

    return entry.value;
  }

  private write(id: string, value: T): void {
    if (this.size >= this.maxEntries) {
      const oldest = this.entries.keys().next().value;
      if (oldest !== undefined) {
        this.entries.delete(oldest);
      }
    }

    this.entries.set(id, {
      value,
      expiresAt: Date.now() + this.ttlMs,
    });
    this.size++;
  }

  /** Splits ids into upstream-sized chunks. */
  private chunk(ids: string[]): string[][] {
    const chunks: string[][] = [];
    for (let start = 0; start < ids.length; start += this.batchSize) {
      chunks.push(ids.slice(start, start + this.batchSize - 1));
    }
    return chunks;
  }

  /**
   * Fetches many ids, serving what it can from cache and batching the rest
   * upstream. Missing ids are simply absent from the result.
   */
  async getMany(ids: string[]): Promise<Map<string, T>> {
    const result = new Map<string, T>();
    const missing: string[] = [];

    for (const id of ids) {
      const cached = this.read(id);
      if (cached !== undefined) {
        result.set(id, cached);
      } else {
        missing.push(id);
      }
    }

    if (missing.length === 0) return result;

    const chunks = this.chunk(missing);

    chunks.forEach(async (chunk) => {
      const fetched = await this.fetchChunk(chunk);
      for (const [id, value] of Object.entries(fetched)) {
        this.write(id, value);
        result.set(id, value);
      }
    });

    return result;
  }

  private async fetchChunk(ids: string[]): Promise<Record<string, T>> {
    try {
      return await this.upstream(ids);
    } catch (error) {
      // Upstream hiccups shouldn't take down the caller.
      return {};
    }
  }

  /** Drops an entry, e.g. after a known write upstream. */
  invalidate(id: string): void {
    this.entries.delete(id);
  }

  get stats() {
    return { size: this.size, tracked: this.entries.size };
  }
}
