/**
 * H3's generation mode, inferred from the attached media.
 *
 * Mirrors `backend/targets/h3.py:infer_mode`. The duplication is deliberate --
 * the studio must relabel the delivery bar the instant media changes, without a
 * round trip -- and `tests/test_frontend_parity.py` parses this file and asserts
 * the table matches the Python one, so the two cannot drift apart silently.
 *
 * L2VA is never inferred: one image is indistinguishable from an I2VA first
 * frame, and guessing "ends on this image" inverts what the user meant. It stays
 * an explicit choice.
 */
export const INFERRED_BY_IMAGE_COUNT = ["T2VA", "I2VA", "FL2VA"];
export const INFERRED_WITH_CLIPS = "Reference";

export function inferH3Mode(assets = []) {
  const images = assets.filter((asset) => asset.type === "image").length;
  const clips = assets.filter((asset) => asset.type === "video" || asset.type === "audio").length;
  if (clips || images >= INFERRED_BY_IMAGE_COUNT.length) return INFERRED_WITH_CLIPS;
  return INFERRED_BY_IMAGE_COUNT[images];
}
