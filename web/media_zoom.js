/**
 * Hover a reference to see it big.
 *
 * The media cards are thumbnails a couple of centimetres across, which is fine
 * for knowing an asset is there and useless for checking what is actually in it
 * -- whether the skirt really is blue, which frame the courier is on. This puts
 * the full prepared image (or a video's contact sheet) on screen at roughly 60%
 * of the viewport while the pointer rests on a card, and takes it away again the
 * moment the pointer leaves.
 *
 * It is deliberately inert: `pointer-events: none`, no focus stealing, no state
 * of its own beyond one timer, so it can never swallow a click on the card
 * underneath or trap the user in a mode.
 */
import { mediaVisualDescriptor } from "./media_visual.js";

// Matches the hover-intent delay in Hub Manager, which does the same thing for
// model rows. Longer than a tooltip on purpose: this covers most of the screen,
// so a premature pop while crossing the grid is genuinely disruptive.
export const HOVER_DELAY_MS = 500;
const CARD_SELECTOR = "[data-asset-id], [data-float-asset]";

const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[character]));

export function assetIdFromCard(card) {
  return card?.dataset?.assetId || card?.dataset?.floatAsset || null;
}

/** Resolves true once the bytes are in, false if they never arrive. */
function loadImage(src, document) {
  return new Promise((resolve) => {
    const image = new document.defaultView.Image();
    image.onload = () => resolve(true);
    image.onerror = () => resolve(false);
    image.src = src;
  });
}

/**
 * @param {object} host
 * @param {HTMLElement} host.root            the studio root; events are delegated from it
 * @param {() => object[]} host.assets       the current asset list
 * @param {() => boolean} [host.suppressed]  true while dragging, so a reorder is not covered
 * @param {number} [host.delay]              hover-intent delay in ms
 * @param {(src: string) => Promise<boolean>} [host.load]  override the image loader
 */
export function createMediaZoom(host) {
  const { root } = host;
  const delay = Number.isFinite(host.delay) ? host.delay : HOVER_DELAY_MS;
  const layer = root.ownerDocument.createElement("div");
  layer.className = "h3ps-media-zoom";
  layer.dataset.mediaZoom = "";
  layer.hidden = true;
  layer.setAttribute("aria-hidden", "true");
  root.appendChild(layer);

  let timer = null;
  let shownFor = null;
  // Bumped on every hover start and every dismissal, so a slow image cannot pop
  // up over a card the pointer has already left.
  let request = 0;

  function hide() {
    clearTimeout(timer);
    timer = null;
    request += 1;
    if (layer.hidden) return;
    layer.hidden = true;
    layer.innerHTML = "";
    shownFor = null;
  }

  async function show(card, token) {
    const id = assetIdFromCard(card);
    const asset = (host.assets() || []).find((item) => item.id === id);
    const visual = mediaVisualDescriptor(asset);
    // Audio has no visual, and an asset still being prepared has no source yet.
    if (!visual?.src) return;
    // Decode first, then reveal: showing the frame before the bytes arrive
    // flashes a big empty box, and an image that never loads should behave
    // exactly as if the feature were not there.
    const load = host.load || ((src) => loadImage(src, root.ownerDocument));
    const loaded = await load(visual.src);
    if (!loaded || token !== request) return;
    const caption = [asset.reference, asset.filename].filter(Boolean).join(" · ");
    layer.innerHTML = `
      <figure>
        <img src="${escape(visual.src)}" alt="">
        ${caption ? `<figcaption>${escape(caption)}</figcaption>` : ""}
      </figure>`;
    layer.hidden = false;
    shownFor = id;
  }

  function schedule(card) {
    const id = assetIdFromCard(card);
    if (!id || id === shownFor) return;
    if (host.suppressed?.()) return;
    clearTimeout(timer);
    request += 1;
    const token = request;
    timer = setTimeout(() => show(card, token), delay);
  }

  const onOver = (event) => {
    const card = event.target.closest?.(CARD_SELECTOR);
    if (card) schedule(card);
    else hide();
  };
  const onOut = (event) => {
    const card = event.target.closest?.(CARD_SELECTOR);
    // Moving within the same card must not flicker it away.
    if (card && card.contains(event.relatedTarget)) return;
    hide();
  };

  root.addEventListener("pointerover", onOver);
  root.addEventListener("pointerout", onOut);
  // Anything that moves the page out from under the preview dismisses it.
  root.addEventListener("dragstart", hide);
  root.addEventListener("wheel", hide, { passive: true });
  root.addEventListener("scroll", hide, true);
  root.addEventListener("keydown", (event) => {
    if (event.key === "Escape") hide();
  });

  return { hide, layer, get visibleFor() { return shownFor; } };
}
