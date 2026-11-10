import test from "node:test";
import assert from "node:assert/strict";
import { bannerText, createHandoff, isEngineNotReady, takeAutostart } from "../web/handoff.js";
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

function fakeRoot() {
  const element = () => ({ hidden: false, textContent: "", addEventListener() {}, querySelector: () => element() });
  const banner = element();
  return { querySelector: () => banner };
}

function fakeFetch(routes) {
  return async (path, options) => {
    const handler = routes[path.replace("/h3studio", "")];
    const [status, body] = handler ? handler(options) : [200, {}];
    return { ok: status < 400, status, json: async () => body };
  };
}

test("autostart connects the preset's LLM after the gate, then restores the caller's target", async () => {
  const calls = [];
  const handoff = createHandoff({
    root: fakeRoot(),
    fetchApi: fakeFetch({
      [`/external/session/${ID}/autostarted`]: () => [200, { first: true }],
      "/fleet-gate/acquire": () => { calls.push("acquire"); return [200, { llm_url: "http://127.0.0.1:8892" }]; },
      [`/external/session/${ID}`]: () => [200, { result: { prompt: "a prompt" } }],
    }),
    isBusy: () => false,
    onError: (title) => calls.push(`error:${title}`),
    retryDelayMs: 1,
  });
  try {
    const ok = await handoff.autostart({
      sessionId: ID,
      session: { external: {}, target: { mode: "Krea2" } },
      refreshModels: async () => calls.push("refresh"),
      connectLlm: async (url) => calls.push(`connect:${url}`),
      selectMode: (mode) => calls.push(`mode:${mode}`),
      hasModel: () => true,
      build: async () => calls.push("build"),
      compile: async () => calls.push("compile"),
    });
    assert.equal(ok, true);
    assert.deepEqual(calls, ["acquire", "refresh", "connect:http://127.0.0.1:8892", "mode:Krea2", "build", "compile"]);
  } finally {
    handoff.stop();
  }
});

test("a failed LLM connect stops the handoff and reports it to the caller", async () => {
  const reported = [];
  const handoff = createHandoff({
    root: fakeRoot(),
    fetchApi: fakeFetch({
      [`/external/session/${ID}/autostarted`]: () => [200, { first: true }],
      "/fleet-gate/acquire": () => [200, { llm_url: "http://127.0.0.1:8892" }],
      [`/external/session/${ID}/error`]: (options) => { reported.push(JSON.parse(options.body).message); return [200, {}]; },
    }),
    isBusy: () => false,
    onError: () => {},
    retryDelayMs: 1,
  });
  try {
    let built = false;
    const ok = await handoff.autostart({
      sessionId: ID,
      session: { external: {}, target: { mode: "Krea2" } },
      refreshModels: async () => {},
      connectLlm: async () => { throw new Error("connection refused"); },
      selectMode: () => {},
      hasModel: () => true,
      build: async () => { built = true; },
      compile: async () => {},
    });
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(ok, false);
    assert.equal(built, false);
    assert.match(reported[0], /Could not connect the local LLM/);
  } finally {
    handoff.stop();
  }
});

function engineHandoff(extraRoutes, reported) {
  return createHandoff({
    root: fakeRoot(),
    fetchApi: fakeFetch({
      [`/external/session/${ID}/autostarted`]: () => [200, { first: true }],
      "/fleet-gate/acquire": () => [200, { llm_url: "http://127.0.0.1:8892" }],
      [`/external/session/${ID}/error`]: (options) => { reported.push(JSON.parse(options.body).message); return [200, {}]; },
      ...extraRoutes,
    }),
    isBusy: () => false,
    onError: () => {},
    retryDelayMs: 1,
  });
}

const notReady = () => Object.assign(new Error("LLM Engine not set - apply the llama.cpp or Strata preset"), { code: "EXTERNAL_SERVER_ERROR" });

test("engine_not_set while the engine loads is retried, not fatal", async () => {
  assert.equal(isEngineNotReady(notReady()), true);
  assert.equal(isEngineNotReady(new Error("model failed")), false);
  const reported = [];
  const handoff = engineHandoff({ [`/external/session/${ID}`]: () => [200, { result: { prompt: "p" } }] }, reported);
  try {
    let builds = 0;
    const ok = await handoff.autostart({
      sessionId: ID, session: { external: {}, target: { mode: "Krea2" } },
      refreshModels: async () => {}, connectLlm: async () => {}, selectMode: () => {}, hasModel: () => true,
      build: async () => { builds += 1; if (builds < 3) throw notReady(); },
      compile: async () => {},
    });
    assert.equal(ok, true);
    assert.equal(builds, 3);
    assert.deepEqual(reported, []);
  } finally {
    handoff.stop();
  }
});

test("a compile that leaves no prompt is reported as an error, not left idle", async () => {
  const reported = [];
  const handoff = engineHandoff({ [`/external/session/${ID}`]: () => [200, { status: "idle", result: null }] }, reported);
  try {
    let compiles = 0;
    const ok = await handoff.autostart({
      sessionId: ID, session: { external: {}, target: { mode: "Krea2" } },
      refreshModels: async () => {}, connectLlm: async () => {}, selectMode: () => {}, hasModel: () => true,
      build: async () => {}, compile: async () => { compiles += 1; },
    });
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(ok, false);
    assert.equal(compiles, 2);
    assert.match(reported[0], /Compile failed/);
  } finally {
    handoff.stop();
  }
});

test("a build that keeps failing for a real reason stops at once and is reported", async () => {
  const reported = [];
  const handoff = engineHandoff({}, reported);
  try {
    let builds = 0;
    const ok = await handoff.autostart({
      sessionId: ID, session: { external: {}, target: { mode: "Krea2" } },
      refreshModels: async () => {}, connectLlm: async () => {}, selectMode: () => {}, hasModel: () => true,
      build: async () => { builds += 1; throw new Error("THINKING_TRUNCATED"); }, compile: async () => {},
    });
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(ok, false);
    assert.equal(builds, 1);
    assert.match(reported[0], /Build failed: THINKING_TRUNCATED/);
  } finally {
    handoff.stop();
  }
});
