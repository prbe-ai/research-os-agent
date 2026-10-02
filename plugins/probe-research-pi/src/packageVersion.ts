/**
 * This package's own version, read from its `package.json` at load time.
 *
 * The MCP client reports it in its `initialize` handshake. It used to be a
 * literal that nobody bumped (`0.1.0` while the package shipped `0.2.0`), so
 * the server could not tell which extension a session ran. Read from the file,
 * the two cannot disagree. `package.json` sits beside `src/` in both install
 * shapes: a checkout and the mirror, which vendors this whole directory.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

/** What an unreadable `package.json` reports: a version no release carries. */
export const UNKNOWN_VERSION = "0.0.0";

export function packageVersion(
  read: (path: string) => string = (path) => readFileSync(path, "utf-8"),
): string {
  try {
    const data: unknown = JSON.parse(read(fileURLToPath(new URL("../package.json", import.meta.url))));
    const version = typeof data === "object" && data !== null ? (data as Record<string, unknown>).version : undefined;
    return typeof version === "string" && version ? version : UNKNOWN_VERSION;
  } catch {
    return UNKNOWN_VERSION;
  }
}
