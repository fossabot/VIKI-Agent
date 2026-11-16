/** Maps one read-only native agent view onto a profile document (pure; no React). */

import type { NativeAssetView } from "../../clients/profiles/nativeAssetsApi";
import { blankProfileDocument, type ProfileDocumentV1 } from "../../clients/profiles/profileApi";

export function profileFromAgent(asset: NativeAssetView): ProfileDocumentV1 {
  const doc = blankProfileDocument();
  doc.identity.name = asset.name;
  doc.identity.purpose = asset.description ?? "";
  // No model is copied: an agent may name a model the catalog does not list (it lists
  // opus[1m], not opus), and checking would need the catalog here, so the user picks a
  // model in the profile editor.
  doc.provenance = {
    kind: "native",
    derived_from: null,
    native_source: {
      source_id: asset.source_id,
      relative_id: asset.relative_id,
      digest: asset.digest,
      observed_at: asset.observed_at,
    },
  };
  return doc;
}
