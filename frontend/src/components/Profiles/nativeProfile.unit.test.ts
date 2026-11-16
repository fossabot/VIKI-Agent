import { describe, expect, it } from "vitest";

import type { NativeAssetView } from "../../clients/profiles/nativeAssetsApi";
import { blankProfileDocument } from "../../clients/profiles/profileApi";
import { profileFromAgent } from "./nativeProfile";

const DIGEST = "a".repeat(64);

function makeAsset(overrides: Partial<NativeAssetView> = {}): NativeAssetView {
  return {
    source_id: "claude:user",
    source_scope: "user",
    provider: "claude",
    relative_id: "agents/example-agent.md",
    kind: "agent",
    name: "example-agent",
    description: "Reviews code.",
    model: "sonnet",
    tools: ["Read", "Grep"],
    digest: DIGEST,
    observed_at: "2026-09-28T02:00:00.000Z",
    supported_fields: [],
    unsupported_fields: [],
    launch_supported: false,
    ...overrides,
  };
}

describe("profileFromAgent", () => {
  it("takes name and purpose from the asset", () => {
    const doc = profileFromAgent(makeAsset());
    expect(doc.identity.name).toBe("example-agent");
    expect(doc.identity.purpose).toBe("Reviews code.");
  });

  it("gives an empty purpose for a null description", () => {
    const doc = profileFromAgent(makeAsset({ description: null }));
    expect(doc.identity.purpose).toBe("");
  });

  it("never copies the asset model, alias or not", () => {
    for (const model of ["sonnet", "opus", "claude-sonnet-5", "INHERIT", "", null]) {
      expect(profileFromAgent(makeAsset({ model })).runtime.model).toBeNull();
    }
  });

  it("builds native provenance from exactly the four source fields", () => {
    const doc = profileFromAgent(makeAsset());
    expect(doc.provenance).toEqual({
      kind: "native",
      derived_from: null,
      native_source: {
        source_id: "claude:user",
        relative_id: "agents/example-agent.md",
        digest: DIGEST,
        observed_at: "2026-09-28T02:00:00.000Z",
      },
    });
  });

  it("never copies the asset tools and keeps instructions empty", () => {
    const doc = profileFromAgent(makeAsset());
    expect(doc.tools).toEqual(blankProfileDocument().tools);
    expect(doc.instructions).toBe("");
  });

  it("returns a fresh document on every call", () => {
    const a = profileFromAgent(makeAsset());
    const b = profileFromAgent(makeAsset());
    expect(a).not.toBe(b);
    expect(a.identity).not.toBe(b.identity);
    expect(a.runtime).not.toBe(b.runtime);
    expect(a.provenance).not.toBe(b.provenance);
    expect(a.tools).not.toBe(b.tools);
  });
});
