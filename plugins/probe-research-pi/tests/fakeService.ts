/**
 * A stand-in Probe service on a real Unix socket, for the `hooks.run` client
 * tests: it reads API v1 lines, answers `service.hello`, and answers
 * `hooks.run` the way the test says (a result, an error, or never).
 */
import { mkdtempSync, rmSync } from "node:fs";
import { createServer, type Server, type Socket } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";

export type Behaviour =
  | { result: Record<string, unknown> }
  | { error: { code: number; message: string } }
  | { silent: true };

export interface FakeService {
  socket: string;
  /** Every request line it read, parsed. */
  requests: Record<string, unknown>[];
  close: () => Promise<void>;
}

/** A private (0700) folder for a socket: `mkdtemp` makes it so. */
export function socketFolder(): string {
  return mkdtempSync(join(tmpdir(), "pi-svc-"));
}

export function answer(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    exit_code: 0,
    stdout: "",
    stderr: "",
    timed_out: false,
    truncated: false,
    stdout_truncated: false,
    stderr_truncated: false,
    via: "host",
    ...overrides,
  };
}

export function denial(reason: string): Record<string, unknown> {
  return answer({
    stdout: JSON.stringify({
      hookSpecificOutput: { hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: reason },
    }),
  });
}

export async function fakeService(socket: string, behaviour: () => Behaviour): Promise<FakeService> {
  rmSync(socket, { force: true });
  const requests: Record<string, unknown>[] = [];
  const open = new Set<Socket>();
  const server: Server = createServer((conn) => {
    open.add(conn);
    conn.on("close", () => open.delete(conn));
    conn.setEncoding("utf8");
    let buffer = "";
    conn.on("data", (chunk: string) => {
      buffer += chunk;
      let at: number;
      while ((at = buffer.indexOf("\n")) >= 0) {
        const request = JSON.parse(buffer.slice(0, at)) as Record<string, unknown>;
        buffer = buffer.slice(at + 1);
        requests.push(request);
        if (request.method === "service.hello") {
          conn.write(JSON.stringify({ jsonrpc: "2.0", id: request.id, result: { api: 1, state: "running" } }) + "\n");
          continue;
        }
        const told = behaviour();
        if ("silent" in told) continue;
        conn.write(JSON.stringify({ jsonrpc: "2.0", id: request.id, ...told }) + "\n");
      }
    });
    conn.on("error", () => {});
  });
  await new Promise<void>((resolve) => server.listen(socket, resolve));
  return {
    socket,
    requests,
    close: () =>
      new Promise<void>((resolve) => {
        for (const conn of open) conn.destroy();
        server.close(() => resolve());
      }),
  };
}
