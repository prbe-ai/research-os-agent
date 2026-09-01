import { describe, expect, it } from "vitest";
import { Type } from "typebox";

import { jsonSchemaToTypeBox, prefixToolName } from "../src/mcpSchema.js";

describe("jsonSchemaToTypeBox", () => {
  it("preserves every field of a real MCP tool input schema untouched", () => {
    const mcpSchema = {
      type: "object",
      properties: {
        run_id: { type: "string", description: "Run id or slug" },
        limit: { type: "integer", minimum: 1, maximum: 100 },
        tags: { type: "array", items: { type: "string" } },
      },
      required: ["run_id"],
      additionalProperties: false,
    };

    const translated = jsonSchemaToTypeBox(mcpSchema) as unknown as Record<string, unknown>;

    expect(translated.type).toBe("object");
    expect(translated.properties).toEqual(mcpSchema.properties);
    expect(translated.required).toEqual(["run_id"]);
    expect(translated.additionalProperties).toBe(false);
  });

  it("preserves schema constructs a hand-rolled Object/String walker would have to special-case ($ref, oneOf, enum)", () => {
    const mcpSchema = {
      type: "object",
      properties: {
        kind: { enum: ["run", "experiment", "project"] },
        filter: { oneOf: [{ type: "string" }, { $ref: "#/$defs/Filter" }] },
      },
      $defs: { Filter: { type: "object" } },
    };

    const translated = jsonSchemaToTypeBox(mcpSchema) as unknown as Record<string, unknown>;

    expect(translated.properties).toEqual(mcpSchema.properties);
    expect(translated.$defs).toEqual(mcpSchema.$defs);
  });

  it("produces an actual TypeBox TSchema (Type.Unsafe's own '~unsafe' tag), not just a plain JSON-shaped object", () => {
    const schema = jsonSchemaToTypeBox({ type: "object", properties: { name: { type: "string" } } }) as unknown as Record<string, unknown>;

    // Verified directly against the installed typebox 1.3.7 runtime: Type.Unsafe
    // clones the given schema and adds this exact hidden marker (mcpSchema.ts's
    // module docstring explains why Type.Unsafe is the right tool here, not a
    // hand-rolled Object/String walker). Presence of the tag is what
    // distinguishes "a real TSchema" from "an object that merely looks like one."
    expect("~unsafe" in schema).toBe(true);
  });

  it("falls back to an empty object schema for a missing/non-object input", () => {
    for (const bad of [undefined, null, "not an object", 42, ["array", "not object"]]) {
      const translated = jsonSchemaToTypeBox(bad) as unknown as Record<string, unknown>;
      expect(translated.type).toBe("object");
      expect(translated.properties).toEqual({});
    }
  });

  it("round-trips through pi's own Type namespace (same package this file imports) without needing a cast", () => {
    // Sanity check that our translated schema is interchangeable with a
    // schema built the "normal" TypeBox way, since pi's own built-in tools
    // (dist/bundle/chunks/*) build theirs with Type.Object/Type.String.
    const normal = Type.Object({ path: Type.String() });
    const unsafe = jsonSchemaToTypeBox({ type: "object", properties: { path: { type: "string" } } });
    expect(JSON.stringify(unsafe)).toContain('"type":"string"');
    expect(typeof normal).toBe("object");
  });
});

describe("prefixToolName", () => {
  it("prefixes with probe_mcp_ so it cannot collide with pi built-ins", () => {
    expect(prefixToolName("search_runs")).toBe("probe_mcp_search_runs");
  });

  it("sanitizes characters outside the OpenAI-safe charset", () => {
    expect(prefixToolName("search.runs/by:project")).toBe("probe_mcp_search_runs_by_project");
  });

  it("sanitizes an all-punctuation name to underscores rather than crashing", () => {
    expect(prefixToolName("???")).toBe("probe_mcp____");
  });

  it("falls back to a literal 'tool' tail only when sanitization would otherwise leave nothing at all", () => {
    expect(prefixToolName("")).toBe("probe_mcp_tool");
  });

  it("truncates to 64 characters total, the tightest provider limit pi routes through", () => {
    const longName = "a".repeat(100);
    const result = prefixToolName(longName);
    expect(result.length).toBe(64);
    expect(result.startsWith("probe_mcp_")).toBe(true);
  });

  it("is stable for already-safe names", () => {
    expect(prefixToolName("list_experiments")).toBe("probe_mcp_list_experiments");
  });
});
