export function mediaVisualDescriptor(asset) {
  if (asset?.type === "video") {
    const w = Number(asset.contact_sheet_width) || 1;
    const h = Number(asset.contact_sheet_height) || 1;
    if (!asset.contact_sheet_url && !asset.sheet) return null;
    return { src: asset.contact_sheet_url, bitmap: asset.sheet, w, h, ar: w / h, kind: "Video sheet" };
  }
  if (asset?.type === "image") {
    const w = Number(asset.width || asset.prepared_width) || 1;
    const h = Number(asset.height || asset.prepared_height) || 1;
    return { src: asset.prepared_url || asset.content_url || asset.preview_url, bitmap: asset.bitmap, w, h, ar: w / h, kind: "Picture" };
  }
  return null;
}

export function referenceCaption(asset) {
  return /^<(Picture|Video) [1-9]\d*>$/.test(asset?.reference || "") ? asset.reference : "";
}

const LABEL_PREFIX = { Picture: "S", Video: "V", Audio: "A" };

/**
 * What the studio calls a reference: S1 rather than <Picture 1>.
 *
 * Display only. `<Picture 1>` is the official MiniMax H3 tag -- the vendored
 * guide requires it and `prompt_audit` rejects a prompt that drops it -- so the
 * canonical form is what the chip inserts and what the compile emits. This is
 * the short label on the card, and the number is the SAME number: S1 is
 * <Picture 1>, never a resequenced position, so the label cannot drift from the
 * tag when media is added or removed.
 */
export function referenceLabel(reference) {
  const match = /^<(Picture|Video|Audio) ([1-9]\d*)>$/.exec(reference || "");
  return match ? `${LABEL_PREFIX[match[1]]}${match[2]}` : reference || "";
}
