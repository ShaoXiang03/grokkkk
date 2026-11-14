import type { DeskSnapshot } from "../shared/types.js";

export interface StreamSink {
  writableLength: number;
  write(chunk: string): boolean;
  end(): void;
}

/**
 * One snapshot for every open tab. The timer runs only while at least one
 * SSE client is connected, and every client receives the same frame.
 */
export class SnapshotHub {
  private clients = new Set<StreamSink>();
  private timer: NodeJS.Timeout | null = null;
  private inflight: Promise<DeskSnapshot> | null = null;
  private latest: DeskSnapshot | null = null;
  private builtAt = 0;
  private frame = "";
  builds = 0;

  constructor(
    private readonly load: () => Promise<DeskSnapshot>,
    readonly intervalMs = 5_000,
  ) {}

  get clientCount(): number {
    return this.clients.size;
  }

  add(client: StreamSink): void {
    this.clients.add(client);
    this.ensure();
    void this.pushOne(client);
  }

  remove(client: StreamSink): void {
    this.clients.delete(client);
    if (this.clients.size === 0) this.stop();
  }

  /** A request for the page. Reuses a snapshot younger than the interval. */
  current(): Promise<DeskSnapshot> {
    return this.refresh(false);
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  private ensure(): void {
    if (this.timer || this.clients.size === 0) return;
    this.timer = setInterval(() => void this.tick(), this.intervalMs);
    this.timer.unref();
  }

  private fresh(): boolean {
    return this.latest != null && Date.now() - this.builtAt < this.intervalMs;
  }

  private async tick(): Promise<void> {
    if (this.clients.size === 0) {
      this.stop();
      return;
    }
    try {
      await this.refresh(true);
    } catch {
      return;
    }
    for (const client of [...this.clients]) this.write(client);
    if (this.clients.size === 0) this.stop();
  }

  private async pushOne(client: StreamSink): Promise<void> {
    try {
      await this.refresh(false);
    } catch {
      return;
    }
    if (this.clients.has(client)) this.write(client);
  }

  private write(client: StreamSink): void {
    if (!this.frame) return;
    if (client.writableLength > 4_000_000) {
      client.end();
      this.clients.delete(client);
      return;
    }
    try {
      client.write(this.frame);
    } catch {
      this.clients.delete(client);
    }
  }

  private refresh(force: boolean): Promise<DeskSnapshot> {
    if (!force && this.fresh() && this.latest) return Promise.resolve(this.latest);
    if (this.inflight) return this.inflight;
    this.inflight = this.load()
      .then((snap) => {
        this.latest = snap;
        this.builtAt = Date.now();
        this.frame = `data: ${JSON.stringify(snap)}\n\n`;
        this.builds += 1;
        return snap;
      })
      .finally(() => {
        this.inflight = null;
      });
    return this.inflight;
  }
}
