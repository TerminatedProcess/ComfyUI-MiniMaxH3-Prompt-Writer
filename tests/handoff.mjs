import test from "node:test";
import assert from "node:assert/strict";
import { bannerText, takeAutostart } from "../web/handoff.js";
import { sessionIdFromUrl } from "../web/studio_state.js";

const ID = "0f8fad5b-d9cb-469f-a165-70867728950e";

test("autostart is taken once: the flag is stripped and the session kept", () => {
  const replaced = [];
  const location = { search: `?session=${ID}&autostart=1`, pathname: "/" };
  assert.equal(takeAutostart(location, { replaceState: (_s, _t, url) => replaced.push(url) }), true);
  assert.deepEqual(replaced, [`/?session=${ID}`]);
  assert.equal(takeAutostart({ search: `?session=${ID}`, pathname: "/" }, { replaceState() {} }), false);
});

test("a handed-over session id wins and is remembered; junk is ignored", () => {
  const store = new Map();
  const storage = { setItem: (k, v) => store.set(k, v), getItem: (k) => store.get(k) };
  assert.equal(sessionIdFromUrl(`?session=${ID}`, storage), ID);
  assert.ok([...store.values()].includes(ID));
  assert.equal(sessionIdFromUrl("?session=../../etc", storage), null);
  assert.equal(sessionIdFromUrl("", storage), null);
});

test("the gate banner says what it is waiting on, and hides otherwise", () => {
  assert.equal(bannerText({ enabled: true, phase: "waiting", message: "InvokeAI has 3 jobs" }),
    "Waiting for the GPU: InvokeAI has 3 jobs.");
  assert.match(bannerText({ enabled: true, phase: "switching", preset: "Prompt Writer + Generator" }), /Prompt Writer \+ Generator/);
  assert.equal(bannerText({ enabled: true, phase: "idle" }), "");
  assert.equal(bannerText({ enabled: false, phase: "waiting" }), "");
});
