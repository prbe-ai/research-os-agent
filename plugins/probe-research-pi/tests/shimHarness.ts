/**
 * `registerExtension(pi, extensionDir)`, the way these tests always called it,
 * now through the real shim (`src/shim.ts`) running the real implementation
 * (`src/impl.ts`) -- so every wiring test also exercises the forwarding.
 *
 * One test FILE stands for one pi process: the implementation's session state
 * used to be module scope in `extension.ts` and outlived each
 * `registerExtension` call (a socket asked once per process, a session spawned
 * once). It lives in the shim's shared state now, so this file carries that
 * state from one call to the next, as pi carries the shim's across a session
 * switch. `extensionDir` is both roots (`installRoot` and `resourceRoot`): no
 * pin is ever named in these tests' state directory.
 *
 * Import it AFTER `vi.mock(...)` of `node:child_process` (dynamically), so the
 * implementation it loads gets the mock.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import * as impl from "../src/impl.js";
import { emptySharedState, ShimHost, type SharedState } from "../src/shim.js";

let shared: SharedState = emptySharedState();

export async function registerExtension(pi: ExtensionAPI, extensionDir: string): Promise<ShimHost> {
  const host = new ShimHost({ installRoot: extensionDir, cloneImpl: impl, shared });
  await host.attach(pi);
  shared = host.sharedState;
  return host;
}
