import assert from "node:assert/strict";
import { createServer } from "node:http";
import { appendFileSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import type { DeskSnapshot } from "../shared/types.js";
import { SnapshotHub, type StreamSink } from "./hub.js";
import { probeHealth } from "./probe.js";
import { readJournalTail } from "./read.js";

function sink(): StreamSink & { chunks: string[] } {
  return {
    chunks: [],
    writableLength: 0,
    write(chunk: string) {
      this.chunks.push(chunk);
      return true;
    },
    end() {},
  };
}

const delay = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

test("one snapshot is built per tick and broadcast to every client", async () => {
  let tick = 0;
  const hub = new SnapshotHub(async () => {
    tick += 1;
    return { now: tick } as DeskSnapshot;
  }, 40);
  const clients = [sink(), sink(), sink()];
  for (const client of clients) hub.add(client);
  await delay(25);
  assert.equal(hub.builds, 1);
  assert.equal(clients[0].chunks.length, 1);
  assert.equal(clients[0].chunks[0], clients[1].chunks[0]);
  assert.equal(clients[1].chunks[0], clients[2].chunks[0]);
  await delay(130);
  assert.ok(hub.builds >= 3 && hub.builds <= 6, `builds=${hub.builds}`);
  assert.equal(clients[0].chunks.length, clients[1].chunks.length);
  assert.equal(clients[0].chunks.at(-1), clients[2].chunks.at(-1));
  for (const client of clients) hub.remove(client);
  assert.equal(hub.clientCount, 0);
  const stopped = hub.builds;
  await delay(120);
  assert.ok(hub.builds - stopped <= 1);
  hub.stop();
});

test("a state read does not keep rebuilding while nobody is streaming", async () => {
  const hub = new SnapshotHub(async () => ({ now: 1 }) as DeskSnapshot, 30);
  await hub.current();
  await hub.current();
  assert.equal(hub.builds, 1);
  await delay(90);
  assert.equal(hub.builds, 1);
  hub.stop();
});

test("journal.jsonl is tailed from the last offset", () => {
  const dir = mkdtempSync(path.join(tmpdir(), "desk-journal-"));
  const file = path.join(dir, "journal.jsonl");
  writeFileSync(file, `${JSON.stringify({ id: 1, ts_ms: 1, event: "signal", payload: {} })}\n{"id":2`);
  const first = readJournalTail(file);
  assert.equal(first.lines, 1);
  assert.equal(first.recent.length, 1);
  appendFileSync(file, `, "event":"fill"}\n`);
  const second = readJournalTail(file);
  assert.equal(second.lines, 2);
  assert.equal(second.recent[1]?.event, "fill");
  const third = readJournalTail(file);
  assert.equal(third.lines, 2);
  assert.equal(third.recent.length, 2);
});

test("health probe reads the body and closes without resetting the server", async () => {
  let connection = "";
  let finished = false;
  let aborted = false;
  const server = createServer((req, res) => {
    connection = String(req.headers.connection ?? "");
    req.on("aborted", () => { aborted = true; });
    res.on("close", () => {
      if (!res.writableFinished) aborted = true;
    });
    res.writeHead(200, { "Content-Type": "application/json", "Content-Length": "11" });
    setTimeout(() => {
      res.end('{"ok":true}');
      finished = true;
    }, 40);
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  const port = address && typeof address === "object" ? address.port : 0;
  try {
    const result = await probeHealth(`http://127.0.0.1:${port}/health`, 2000);
    assert.equal(result.ok, true);
    assert.equal(result.status, 200);
    assert.equal(connection, "close");
    assert.equal(finished, true);
    await delay(30);
    assert.equal(aborted, false);
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
});
