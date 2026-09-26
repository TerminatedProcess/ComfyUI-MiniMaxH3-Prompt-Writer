import assert from "node:assert/strict";
import test from "node:test";
import { Window } from "happy-dom";
import { readFile } from "node:fs/promises";
import { assetIdFromCard, createMediaZoom, HOVER_DELAY_MS } from "../web/media_zoom.js";

const read = (path) => readFile(new URL(path, import.meta.url), "utf8");

const IMAGE = {
  id: "a1",
  type: "image",
  reference: "<Picture 1>",
  filename: "hero.png",
  width: 1600,
  height: 900,
  prepared_url: "/prepared/hero.jpg",
  preview_url: "/preview/hero.jpg",
};
const VIDEO = {
  id: "a2",
  type: "video",
  reference: "<Video 1>",
  filename: "clip.mp4",
  contact_sheet_url: "/sheet/clip.jpg",
  contact_sheet_width: 1200,
  contact_sheet_height: 800,
};
const AUDIO = { id: "a3", type: "audio", reference: "<Audio 1>", filename: "score.wav" };

function mount({ assets = [IMAGE, VIDEO, AUDIO], suppressed = () => false, delay = 0, load } = {}) {
  const window = new Window();
  const root = window.document.createElement("div");
  root.innerHTML = `
    <div class="h3ps-asset" data-asset-id="a1"><span class="h3ps-asset-preview"><img class="h3ps-real-thumb"></span></div>
    <div class="h3ps-asset" data-asset-id="a2"><span class="h3ps-asset-preview"></span></div>
    <div class="h3ps-asset" data-asset-id="a3"><span class="h3ps-wave"></span></div>
    <div class="h3ps-asset" data-float-asset="a1"><span class="h3ps-asset-preview"></span></div>
    <p data-not-a-card>elsewhere</p>`;
  window.document.body.append(root);
  const loads = [];
  const zoom = createMediaZoom({
    root,
    assets: () => assets,
    suppressed,
    delay,
    // happy-dom does not fetch, so the loader is injected; the real one waits
    // for the image to decode before revealing anything.
    load: load || (async (src) => { loads.push(src); return true; }),
  });
  const over = (selector) => {
    const target = root.querySelector(selector);
    target.dispatchEvent(new window.Event("pointerover", { bubbles: true }));
    return target;
  };
  const out = (selector) => {
    const target = root.querySelector(selector);
    const event = new window.Event("pointerout", { bubbles: true });
    event.relatedTarget = null;
    target.dispatchEvent(event);
  };
  const settle = () => new Promise((resolve) => setTimeout(resolve, delay + 5));
  return { window, root, zoom, over, out, settle, loads };
}

test("hovering a picture shows it at the prepared size, captioned", async () => {
  const { root, zoom, over, settle, window } = mount();
  over('[data-asset-id="a1"]');
  await settle();
  assert.equal(zoom.layer.hidden, false);
  assert.equal(zoom.visibleFor, "a1");
  assert.equal(root.querySelector("[data-media-zoom] img").getAttribute("src"), "/prepared/hero.jpg");
  assert.match(root.querySelector("[data-media-zoom] figcaption").textContent, /<Picture 1> · hero\.png/);
  await window.happyDOM.close();
});

test("hovering a video shows its contact sheet, not a single frame", async () => {
  const { root, over, settle, window } = mount();
  over('[data-asset-id="a2"]');
  await settle();
  assert.equal(root.querySelector("[data-media-zoom] img").getAttribute("src"), "/sheet/clip.jpg");
  await window.happyDOM.close();
});

test("audio has nothing to show and is left alone", async () => {
  const { zoom, over, settle, window } = mount();
  over('[data-asset-id="a3"]');
  await settle();
  assert.equal(zoom.layer.hidden, true);
  await window.happyDOM.close();
});

test("leaving the card takes the preview away", async () => {
  const { zoom, over, out, settle, window } = mount();
  over('[data-asset-id="a1"]');
  await settle();
  assert.equal(zoom.layer.hidden, false);
  out('[data-asset-id="a1"]');
  assert.equal(zoom.layer.hidden, true);
  assert.equal(zoom.layer.innerHTML, "");
  await window.happyDOM.close();
});

test("moving inside the same card does not flicker it", async () => {
  const { root, zoom, over, settle, window } = mount();
  const card = over('[data-asset-id="a1"]');
  await settle();
  const event = new window.Event("pointerout", { bubbles: true });
  event.relatedTarget = root.querySelector('[data-asset-id="a1"] img');
  card.dispatchEvent(event);
  assert.equal(zoom.layer.hidden, false);
  await window.happyDOM.close();
});

test("a drag is never covered by the preview", async () => {
  const { zoom, over, settle, window } = mount({ suppressed: () => true });
  over('[data-asset-id="a1"]');
  await settle();
  assert.equal(zoom.layer.hidden, true);
  await window.happyDOM.close();
});

test("scrolling, dragging and Escape all dismiss it", async () => {
  for (const fire of [
    (root, window) => root.dispatchEvent(new window.Event("dragstart", { bubbles: true })),
    (root, window) => root.dispatchEvent(new window.Event("wheel", { bubbles: true })),
    (root, window) => root.dispatchEvent(new window.Event("scroll", { bubbles: true })),
    (root, window) => root.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true })),
  ]) {
    const { root, zoom, over, settle, window } = mount();
    over('[data-asset-id="a1"]');
    await settle();
    assert.equal(zoom.layer.hidden, false);
    fire(root, window);
    assert.equal(zoom.layer.hidden, true);
    await window.happyDOM.close();
  }
});

test("hovering anything that is not a card closes an open preview", async () => {
  const { zoom, over, settle, window } = mount();
  over('[data-asset-id="a1"]');
  await settle();
  over("[data-not-a-card]");
  assert.equal(zoom.layer.hidden, true);
  await window.happyDOM.close();
});

test("the floating media panel's cards work too", async () => {
  const { zoom, over, settle, window } = mount();
  over("[data-float-asset]");
  await settle();
  assert.equal(zoom.visibleFor, "a1");
  await window.happyDOM.close();
});

test("an asset that is gone shows nothing rather than an empty frame", async () => {
  const { zoom, over, settle, window } = mount({ assets: [] });
  over('[data-asset-id="a1"]');
  await settle();
  assert.equal(zoom.layer.hidden, true);
  await window.happyDOM.close();
});

test("the preview cannot intercept a click on the card underneath", async () => {
  const css = await read("../web/styles/media.css");
  const block = css.slice(css.indexOf(".h3ps-media-zoom {"));
  assert.match(block, /pointer-events: none/);
  // Roughly 60% of the viewport, as asked for.
  assert.match(block, /max-width: 60vw/);
  assert.match(block, /max-height: 60vh/);
});

test("an image that never loads behaves as if the feature were not there", async () => {
  const { zoom, over, settle, window } = mount({ load: async () => false });
  over('[data-asset-id="a1"]');
  await settle();
  assert.equal(zoom.layer.hidden, true);
  await window.happyDOM.close();
});

test("a slow image cannot pop up over a card the pointer has already left", async () => {
  // Hub Manager's request token, same reasoning: the load outlives the hover.
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const { zoom, over, out, settle, window } = mount({ load: () => gate });
  over('[data-asset-id="a1"]');
  await settle();
  out('[data-asset-id="a1"]');
  release(true);
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal(zoom.layer.hidden, true, "the preview must not appear after the pointer left");
  await window.happyDOM.close();
});

test("nothing is revealed before the bytes are in", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const { zoom, over, settle, window } = mount({ load: () => gate });
  over('[data-asset-id="a1"]');
  await settle();
  assert.equal(zoom.layer.hidden, true, "no empty frame while the image loads");
  release(true);
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal(zoom.layer.hidden, false);
  await window.happyDOM.close();
});

test("the hover delay is long enough not to flash while crossing the grid", () => {
  assert.ok(HOVER_DELAY_MS >= 300 && HOVER_DELAY_MS <= 600, String(HOVER_DELAY_MS));
  assert.equal(assetIdFromCard({ dataset: { assetId: "x" } }), "x");
  assert.equal(assetIdFromCard({ dataset: { floatAsset: "y" } }), "y");
  assert.equal(assetIdFromCard(null), null);
});

test("the studio wires the zoom, suppresses it mid-drag and drops it on re-render", async () => {
  const main = await read("../web/main.js");
  assert.match(main, /import \{ createMediaZoom \} from "\.\/media_zoom\.js";/);
  assert.match(main, /studio\.mediaZoom = createMediaZoom\(\{/);
  assert.match(main, /suppressed: \(\) => Boolean\(studio\.draggedAssetId\)/);
  // A removed card never fires pointerout, so renderMedia has to say so.
  assert.match(main, /studio\.mediaZoom\?\.hide\(\);/);
});
