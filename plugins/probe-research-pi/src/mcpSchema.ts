/**
 * Translating an MCP tool into a pi tool: its JSON Schema `inputSchema` into
 * a TypeBox `TSchema` for `pi.registerTool({ parameters })`, and its name
 * into something namespaced and provider-safe.
 *
 * WHY `Type.Unsafe`, NOT A HAND-WRITTEN RECURSIVE TRANSLATOR. Verified
 * against pi 0.86.0's installed source (`node_modules/@earendil-works/pi-coding-agent`
 * and the `pi-ai` it ships; both files below are the same in 0.84.3):
 * `tool.parameters` is used at runtime for three things, and all three read
 * it as a plain JSON Schema object.
 *   1. `getJsonSchemaToolParameters()` (`pi-ai`'s `dist/api/constrained-sampling.js`,
 *      bundled into the CLI) hands `tool.parameters` to the model provider AS
 *      a plain JSON Schema object. Only a tool that sets `constrainedSampling`
 *      (pi's own built-ins; never these) gets `makeStrictJsonSchema()` instead.
 *   2. `validateToolArguments()` (`pi-ai`'s `dist/utils/validation.js`) checks
 *      every call's arguments before `execute` runs: `Value.Convert`, then
 *      `Compile(tool.parameters).Check`. TypeBox 1.x compiles raw JSON Schema,
 *      so a `Type.Unsafe` schema validates exactly as the server wrote it
 *      (checked against pi 0.86.0: `"5"` for an integer is coerced to 5; a
 *      missing required key, a bad enum value, an extra key under
 *      `additionalProperties: false` and a value under `minimum` are all
 *      rejected).
 *   3. `Static<TParams>` — a compile-time-only TypeScript mechanism, gone by
 *      the time anything runs.
 *   So the one thing an incoming TSchema actually needs to be is a faithful,
 *   complete JSON Schema object — which is precisely what TypeBox's own
 *   `Type.Unsafe<T>(schema)` produces: it clones the given schema and tags
 *   it (via `Memory.Update`, verified in the installed `typebox` 1.3.7
 *   source) without touching a single one of its keys. A hand-rolled
 *   Object/String/Array walker would have to reinvent `$ref`, `oneOf`,
 *   `anyOf`, `enum`, `additionalProperties`, etc. to stay faithful to
 *   whatever the MCP server sends — `Type.Unsafe` already gets all of it
 *   right, for free, because it never has to understand the schema at all.
 *
 * MCP tool schemas are always object-rooted per the spec (confirmed against
 * `@modelcontextprotocol/sdk`'s own `Client.listTools()` return type:
 * `inputSchema: { type: "object", properties?, required? }`), so a missing
 * schema — tolerated defensively, not expected — falls back to an empty
 * object schema rather than `undefined`.
 */

import { Type, type TSchema } from "typebox";

/** Wrap an MCP tool's JSON Schema `inputSchema` as a TypeBox `TSchema`. See module docstring. */
export function jsonSchemaToTypeBox(schema: unknown): TSchema {
  const base =
    schema && typeof schema === "object" && !Array.isArray(schema) ? (schema as Record<string, unknown>) : { type: "object", properties: {} };
  return Type.Unsafe<Record<string, unknown>>(base);
}

/**
 * Every registered tool name is prefixed so it can never collide with a pi
 * built-in (`read`, `edit`, `bash`, ...) or another extension's tool, and
 * sanitized to the character set every provider we route through actually
 * accepts (OpenAI's function-name pattern, `^[a-zA-Z0-9_-]{1,64}$`, is the
 * tightest of the ones pi supports, so it is the one this targets).
 */
export const TOOL_NAME_PREFIX = "probe_mcp_";
const MAX_TOOL_NAME_LENGTH = 64;

export function prefixToolName(mcpToolName: string): string {
  const sanitized = mcpToolName.replace(/[^a-zA-Z0-9_-]/g, "_") || "tool";
  const full = `${TOOL_NAME_PREFIX}${sanitized}`;
  return full.length > MAX_TOOL_NAME_LENGTH ? full.slice(0, MAX_TOOL_NAME_LENGTH) : full;
}
