import assert from "node:assert/strict";
import { Window } from "happy-dom";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { INFERRED_BY_IMAGE_COUNT, INFERRED_WITH_CLIPS, inferH3Mode } from "../web/mode_inference.js";

const read = (path) => readFile(new URL(path, import.meta.url), "utf8");
const stage = await read("../web/writer_stage.js");
const main = await read("../web/main.js");
const stateSource = await read("../web/studio_state.js");
const { buildGeneratePayload, isGenerationModeAvailable, stagePayload } =
  await import(`data:text/javascript;base64,${Buffer.from(stateSource).toString("base64")}`);

// Import the stage for real, with the ComfyUI-bound API module stubbed out. A
// source-text check cannot catch a constant that went missing in a refactor --
// that shipped a stage whose goals panel threw at runtime while every regex
// assertion still passed.
const apiStub = `data:text/javascript;base64,${Buffer.from(`
  export let lastCall = null;
  export const getTargets = async () => globalThis.__stageRegistry;
  export const getSession = async () => ({ session: globalThis.__stageSession });
  export const buildGeneric = async (body) => { lastCall = ["build", body]; return { session: globalThis.__stageSession }; };
  export const sendGenericTurn = async (body) => { lastCall = ["turn", body]; return { session: globalThis.__stageSession }; };
  export const editGenericField = async (body) => { lastCall = ["field", body]; return { session: globalThis.__stageSession }; };
  export const changeGoal = async (body) => { lastCall = ["goal", body]; return { session: globalThis.__stageSession }; };
  export const saveInputs = async (body) => { lastCall = ["inputs", body]; return { session: globalThis.__stageSession }; };
  export const resetSession = async (body) => { lastCall = ["reset", body]; return { session: globalThis.__stageSession }; };
`).toString("base64")}`;
const inferenceStub = `data:text/javascript;base64,${Buffer.from(
  await read("../web/mode_inference.js"),
).toString("base64")}`;
const stageModule = await import(`data:text/javascript;base64,${Buffer.from(
  stage
    .replace('"./api/h3studio.js"', JSON.stringify(apiStub))
    .replace('"./mode_inference.js"', JSON.stringify(inferenceStub)),
).toString("base64")}`);

const image = { type: "image" };
const video = { type: "video" };
const audio = { type: "audio" };

test("the mode follows what is attached, and never guesses L2VA", () => {
  assert.equal(inferH3Mode([]), "T2VA");
  assert.equal(inferH3Mode([image]), "I2VA");
  assert.equal(inferH3Mode([image, image]), "FL2VA");
  assert.equal(inferH3Mode([image, image, image]), "Reference");
  assert.equal(inferH3Mode([video]), "Reference");
  assert.equal(inferH3Mode([audio]), "Reference");
  assert.equal(inferH3Mode([image, video]), "Reference");
  assert.deepEqual(INFERRED_BY_IMAGE_COUNT, ["T2VA", "I2VA", "FL2VA"]);
  assert.equal(INFERRED_WITH_CLIPS, "Reference");
  assert.ok(!INFERRED_BY_IMAGE_COUNT.includes("L2VA"));
});

test("a compile carries the generic document, the goals and both flags", () => {
  const session = {
    inputs: { nsfw: false, story: true },
    goals: [{ id: "g1", text: "keep it one shot", enabled: true }],
    generic: { schema: "generic/1", fields: { subject: { value: "Bob", origin: "user" } } },
    fields: { subject: { value: "Bob", origin: "user" }, mood: { value: "", origin: "unspecified" } },
    target: { mode: "Anima", variant: "aesthetic" },
  };
  // Mirrors the stage's real export: `session` is a function, not a property.
  const state = { stage: { session: () => session, variantsFor: () => ["base", "aesthetic", "turbo"] }, mode: "Anima" };
  const payload = stagePayload(state);
  assert.equal(payload.nsfw, false);
  assert.equal(payload.story, true);
  assert.equal(payload.session_media, true);
  assert.equal(payload.variant, "aesthetic");
  assert.equal(payload.goals.length, 1);
  assert.equal(payload.generic.fields.subject.value, "Bob");
});

test("an empty document is not sent, so a brief-only compile still works", () => {
  const session = {
    inputs: {},
    goals: [],
    generic: { schema: "generic/1", fields: {} },
    fields: { subject: { value: "", origin: "unspecified" } },
    target: {},
  };
  const payload = stagePayload({ stage: { session: () => session, variantsFor: () => [] }, mode: "Krea2" });
  assert.equal(payload.generic, undefined);
  assert.equal(payload.session_media, true);
  assert.equal(payload.variant, undefined);
});

test("the payload reads the stage's session the way the stage exports it", async () => {
  // Guards the exact slip that made the whole two-stage flow a no-op: a getter
  // read as a property is truthy, so every field silently came back undefined.
  const window = new Window();
  const previousDocument = globalThis.document;
  globalThis.document = window.document;
  try {
    const root = mountRoot(window);
    globalThis.__stageRegistry = REGISTRY;
    globalThis.__stageSession = SESSION;
    const stageInstance = stageModule.createWriterStage({
      root, icon: () => "", sessionId: () => "11111111-2222-4333-8444-555555555555",
      assets: () => [], briefValue: () => "", currentMode: () => "Anima",
      inferencePayload: () => ({}), requestBusy: () => false, selectMode: () => {},
      inferredModeSummary: () => "", compile: () => {}, setStatus: () => {}, clearStatus: () => {},
      notify: () => {}, notifyError: () => {}, confirmReset: () => false, afterReset: () => {},
      onSessionChanged: () => {}, outputIsReplaceable: () => true, afterOutputReplaced: () => {}, copy: () => {},
    });
    await stageInstance.start();
    const payload = stagePayload({ stage: stageInstance, mode: "Anima" });
    assert.equal(payload.session_media, true);
    assert.equal(payload.goals.length, SESSION.goals.length);
    assert.ok(payload.generic, "the document must reach the compile");
    assert.equal(payload.nsfw, true);
    assert.equal(payload.story, false);
  } finally {
    globalThis.document = previousDocument;
    await window.happyDOM.close();
  }
});

test("without the stage the payload is unchanged from before", () => {
  assert.deepEqual(stagePayload({ mode: "T2VA" }), {});
  const payload = buildGeneratePayload({ mode: "T2VA", sessionId: "s", durationSeconds: 10, aspectRatio: "16:9", customSystemPrompts: {} },
    { creativeBrief: "a brief", seed: 1 });
  assert.equal(payload.generic, undefined);
  assert.equal(payload.session_media, undefined);
  assert.equal(payload.creative_brief, "a brief");
});

test("vision is gated on attachments, not on the chosen mode", () => {
  const textOnly = { family: "gguf", capabilities: { images: false } };
  const vision = { family: "gguf", capabilities: { images: true } };
  assert.equal(isGenerationModeAvailable(textOnly, "Reference", 0), true);
  assert.equal(isGenerationModeAvailable(textOnly, "Reference", 2), false);
  assert.equal(isGenerationModeAvailable(textOnly, "Krea2", 0), true);
  assert.equal(isGenerationModeAvailable(vision, "Reference", 3), true);
});

const SESSION = {
  schema: "session/1",
  labels: { subject: "Subject", wardrobe: "Wardrobe", mood: "Mood" },
  field_order: ["subject", "wardrobe", "mood"],
  fields: {
    subject: { value: "A courier", origin: "user", observed: null },
    wardrobe: { value: "a red skirt", origin: "override", observed: "a blue skirt" },
    mood: { value: "", origin: "unspecified", observed: null },
  },
  groups: [{ title: "Subject", fields: ["subject", "wardrobe", "mood"] }],
  goals: [
    { id: "g1", text: "keep her skirt red", kind: "presence", enabled: true, verdict: "met", reason: "present in the prompt" },
    { id: "g2", text: "not a commercial", kind: "judged", enabled: false, verdict: "pending", reason: "" },
    { id: "g3", text: "colours from the image", kind: "field", enabled: true, verdict: "unmet", reason: "Wardrobe is still unspecified" },
  ],
  conversation: [
    { role: "user", text: "her skirt is red", changed: [], protected: [], goals_added: [] },
    { role: "assistant", text: "Done.", changed: ["wardrobe"], protected: ["subject"], goals_added: ["keep her skirt red"] },
  ],
  inputs: { nsfw: true, story: false, brief: "a courier" },
  outputs: { Krea2: { prompt: "a prose prompt", negative_prompt: "", audit: {}, generic_updated_at: 1 } },
  target: { mode: "Krea2", variant: null },
  media_missing: [{ filename: "hero.png" }],
  unspecified: ["mood"],
  generic: { updated_at: 1 },
};

const REGISTRY = {
  targets: [
    {
      id: "h3", label: "MiniMax H3", workspace: "video", infers_mode: true, variants: [], fields: [],
      modes: [
        { id: "T2VA", label: "From a description", summary: "from your description alone", generation: true },
        { id: "Reference", label: "Using references", summary: "using your images as references", generation: true },
      ],
    },
    {
      id: "krea2", label: "Krea 2", workspace: "image", infers_mode: false, variants: [], fields: [],
      modes: [{ id: "Krea2", label: "Krea 2 image prompt", summary: "as one Krea 2 prose prompt", generation: true }],
    },
    {
      id: "anima", label: "Anima", workspace: "image", infers_mode: false,
      variants: ["base", "aesthetic", "turbo"], default_variant: "turbo", fields: ["negative_prompt"],
      modes: [{ id: "Anima", label: "Anima tag prompt", summary: "as an Anima tag pair", generation: true }],
    },
    {
      id: "music3", label: "MiniMax Music 3", workspace: "music", infers_mode: false, variants: [], fields: [],
      modes: [{ id: "Music3", label: "Structured caption", summary: "", generation: true }],
    },
  ],
};

/** The parts of the studio DOM the stage injects itself into. */
function mountRoot(window) {
  const root = window.document.createElement("div");
  root.innerHTML = `
    <section class="h3ps-input-panel">
      <div data-video-inputs>
        <div class="h3ps-control-grid" data-delivery-controls>
          <label class="h3ps-field" data-duration-field><input data-duration-slider></label>
          <label class="h3ps-field h3ps-choice"><button data-choice-toggle="aspect"></button></label>
        </div>
        <label class="h3ps-brief"><textarea data-video-brief></textarea></label>
      </div>
    </section>
    <section class="h3ps-output-panel">
      <div class="h3ps-editor-wrap"><textarea data-output></textarea></div>
      <div class="h3ps-output-actions"></div>
    </section>`;
  window.document.body.append(root);
  return root;
}

test("the stage mounts and renders every panel without an undefined reference", async () => {
  const window = new Window();
  const previousDocument = globalThis.document;
  globalThis.document = window.document;
  try {
    const root = mountRoot(window);
    const calls = [];
    const stageInstance = stageModule.createWriterStage({
      root,
      icon: () => "<svg></svg>",
      sessionId: () => "11111111-2222-4333-8444-555555555555",
      assets: () => [{ type: "image" }],
      briefValue: () => "a courier",
      currentMode: () => "Krea2",
      inferencePayload: () => ({}),
      requestBusy: () => false,
      selectMode: (mode) => calls.push(["selectMode", mode]),
      inferredModeSummary: () => "",
      compile: () => calls.push(["compile"]),
      setStatus: () => {},
      clearStatus: () => {},
      notify: () => {},
      notifyError: (error) => calls.push(["error", error.message]),
      confirmReset: () => false,
      afterReset: () => {},
      onSessionChanged: () => {},
      outputIsReplaceable: () => true,
      afterOutputReplaced: () => {},
      copy: () => {},
    });

    globalThis.__stageRegistry = REGISTRY;
    globalThis.__stageSession = SESSION;
    await stageInstance.start();
    const html = root.innerHTML;
    assert.deepEqual(calls.filter(([kind]) => kind === "error"), [], "rendering must not throw");
    // Origin chips, goal marks and goal-kind hints are all looked up by name:
    // a constant lost in a refactor only shows up here, never in a source regex.
    assert.match(html, /your choice/);
    assert.match(html, /image shows: a blue skirt/);
    assert.match(html, /not specified/);
    assert.match(html, /keep her skirt red/);
    assert.match(html, /checked by looking for it in the compiled prompt/);
    assert.match(html, /Wardrobe is still unspecified/);
    assert.match(html, /hero\.png/);
    assert.match(html, /Automatic/);
    assert.match(html, /Generate Krea 2 prompt/);
    assert.equal(root.querySelectorAll(".h3ps-stage-turn").length, 2);
    assert.equal(root.querySelector("[data-stage-flag=\"story\"]").checked, false);
    assert.equal(root.querySelector("[data-stage-flag=\"nsfw\"]").checked, true);
  } finally {
    globalThis.document = previousDocument;
    await window.happyDOM.close();
  }
});

test("the controls on the card do what they say", async () => {
  const window = new Window();
  const previousDocument = globalThis.document;
  globalThis.document = window.document;
  try {
    const root = mountRoot(window);
    const calls = [];
    const stageInstance = stageModule.createWriterStage({
      root,
      icon: () => "<svg></svg>",
      sessionId: () => "11111111-2222-4333-8444-555555555555",
      assets: () => [],
      briefValue: () => "a courier",
      currentMode: () => "Krea2",
      inferencePayload: () => ({ model_id: "m" }),
      requestBusy: () => false,
      selectMode: (mode) => calls.push(["selectMode", mode]),
      inferredModeSummary: () => "",
      compile: () => calls.push(["compile"]),
      setStatus: () => {},
      clearStatus: () => {},
      notify: () => {},
      notifyError: (error) => calls.push(["error", error.message]),
      confirmReset: () => false,
      afterReset: () => calls.push(["afterReset"]),
      onSessionChanged: () => {},
      outputIsReplaceable: () => true,
      afterOutputReplaced: () => {},
      copy: () => calls.push(["copy"]),
    });
    globalThis.__stageRegistry = REGISTRY;
    globalThis.__stageSession = SESSION;
    await stageInstance.start();
    const api = await import(apiStub);

    root.querySelector('[data-stage-flag="story"]').checked = true;
    root.querySelector('[data-stage-flag="story"]').dispatchEvent(new window.Event("change", { bubbles: true }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.deepEqual(api.lastCall[0], "inputs");
    assert.equal(api.lastCall[1].story, true);

    root.querySelector('[data-goal-id="g1"] [data-goal-toggle]').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.deepEqual(api.lastCall[1], { session_id: "11111111-2222-4333-8444-555555555555", action: "toggle", id: "g1", enabled: false });

    root.querySelector('[data-goal-id="g3"] [data-goal-delete]').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(api.lastCall[1].action, "delete");

    // Inline edit: click the value, type, commit with Enter.
    root.querySelector('[data-field="wardrobe"] [data-generic-value]').click();
    const input = root.querySelector(".h3ps-generic-input");
    assert.ok(input, "clicking a value opens an editor");
    input.value = "a green skirt";
    input.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.deepEqual(api.lastCall[0], "field");
    assert.equal(api.lastCall[1].value, "a green skirt");

    // Switching the model must relabel the button and must not touch the brief.
    const picker = root.querySelector("[data-delivery-target]");
    picker.value = "Anima";
    picker.dispatchEvent(new window.Event("change", { bubbles: true }));
    assert.ok(calls.some(([kind, mode]) => kind === "selectMode" && mode === "Anima"));

    root.querySelector("[data-stage-compile]").click();
    assert.ok(calls.some(([kind]) => kind === "compile"));

    // Reset asks first, and a refusal changes nothing.
    root.querySelector("[data-stage-reset]").click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.ok(!calls.some(([kind]) => kind === "afterReset"));
  } finally {
    globalThis.document = previousDocument;
    await window.happyDOM.close();
  }
});

test("the middle column holds the brief, the flags, Generate and the conversation", async () => {
  const window = new Window();
  const previousDocument = globalThis.document;
  globalThis.document = window.document;
  try {
    const root = mountRoot(window);
    globalThis.__stageRegistry = REGISTRY;
    globalThis.__stageSession = SESSION;
    const stageInstance = stageModule.createWriterStage({
      root, icon: () => "", sessionId: () => "11111111-2222-4333-8444-555555555555",
      assets: () => [], briefValue: () => "", currentMode: () => "Krea2",
      inferencePayload: () => ({}), requestBusy: () => false, selectMode: () => {},
      inferredModeSummary: () => "", compile: () => {}, setStatus: () => {}, clearStatus: () => {},
      notify: () => {}, notifyError: () => {}, confirmReset: () => false, afterReset: () => {},
      onSessionChanged: () => {}, outputIsReplaceable: () => true, afterOutputReplaced: () => {},
      copy: () => {}, applyInputs: () => {},
    });
    await stageInstance.start();

    const author = root.querySelector("[data-author-panel]");
    assert.ok(author, "the authoring column exists");
    // References stay left; the brief and the conversation move to the middle.
    assert.ok(author.querySelector(".h3ps-brief"), "the brief moved into the authoring column");
    assert.ok(author.querySelector("[data-stage-log]"), "the conversation is in the authoring column");
    assert.ok(!root.querySelector("[data-video-inputs] .h3ps-brief"), "the brief left the media column");
    // Order down the column: flags, brief, Generate, conversation.
    const order = [...author.querySelectorAll(".h3ps-stage-flags, .h3ps-brief, .h3ps-stage-actions, .h3ps-stage-chat")]
      .map((node) => node.className.split(" ")[0]);
    assert.deepEqual(order, ["h3ps-stage-flags", "h3ps-brief", "h3ps-stage-actions", "h3ps-stage-chat"]);
    // Duration and aspect ratio now sit with the model, not with the media.
    const slot = root.querySelector("[data-delivery-controls-slot]");
    assert.ok(slot.querySelector("[data-duration-field]"), "duration moved to the delivery bar");
    assert.ok(!root.querySelector("[data-video-inputs] [data-delivery-controls]"));
    // Krea 2 has no runtime, so the duration control is hidden for it.
    assert.equal(root.querySelector("[data-duration-field]").hidden, true);
  } finally {
    globalThis.document = previousDocument;
    await window.happyDOM.close();
  }
});

test("the workspace only goes to three columns for the writer flow", () => {
  assert.match(main, /const threeColumn = Boolean\(studio\.stage\) && !music && !studio\.sequence\?\.active;/);
  assert.match(main, /classList\.toggle\("is-three-column", threeColumn\)/);
});

test("the stage owns the flags, the build, the conversation and the delivery bar", () => {
  for (const hook of [
    "data-stage-flag=\"nsfw\"",
    "data-stage-flag=\"story\"",
    "data-stage-build",
    "data-stage-send",
    "data-stage-reset",
    "data-stage-compile",
    "data-delivery-target",
    "data-delivery-variant-select",
    "data-goal-list",
    "data-generic-groups",
    "data-negative-output",
  ]) {
    assert.ok(stage.includes(hook), hook);
  }
});

test("the generic card explains every origin, including a deliberate override", () => {
  for (const origin of ["asset", "user", "override", "invented", "unspecified"]) {
    assert.match(stage, new RegExp(`${origin}:\\s*\\{ label:`));
  }
  assert.match(stage, /your choice/);
  assert.match(stage, /image shows: /);
});

test("a prompt compiled before the last change says so", async () => {
  const window = new Window();
  const previousDocument = globalThis.document;
  globalThis.document = window.document;
  try {
    const root = mountRoot(window);
    globalThis.__stageRegistry = REGISTRY;
    // The stored Krea 2 output came from an older version of the document.
    globalThis.__stageSession = { ...SESSION, generic: { updated_at: 2 } };
    const stageInstance = stageModule.createWriterStage({
      root, icon: () => "", sessionId: () => "11111111-2222-4333-8444-555555555555",
      assets: () => [], briefValue: () => "", currentMode: () => "Krea2",
      inferencePayload: () => ({}), requestBusy: () => false, selectMode: () => {},
      inferredModeSummary: () => "as one Krea 2 prose prompt", compile: () => {},
      setStatus: () => {}, clearStatus: () => {}, notify: () => {}, notifyError: () => {},
      confirmReset: () => false, afterReset: () => {}, onSessionChanged: () => {},
      outputIsReplaceable: () => true, afterOutputReplaced: () => {}, copy: () => {},
    });
    await stageInstance.start();
    const note = root.querySelector("[data-delivery-mode]");
    assert.equal(note.dataset.stale, "true");
    assert.match(note.textContent, /compiled before your last change/);
  } finally {
    globalThis.document = previousDocument;
    await window.happyDOM.close();
  }
});

test("choosing a different model never rewrites the brief", () => {
  // The per-mode draft swap belongs to the old one-prompt-per-mode studio; with
  // one generic prompt driving every target it would silently replace what the
  // user typed the moment they switched models.
  const selectMode = main.match(/selectMode: \(mode, \{ silent = false \} = \{\}\) => \{[\s\S]*?\n    \},/);
  assert.ok(selectMode, "the stage's selectMode host callback is still there");
  assert.doesNotMatch(selectMode[0], /restoreModeDraft|stashCurrentModeDraft/);
  assert.match(selectMode[0], /syncWorkspace\(\)/);
});

test("an unavailable stage falls back to the classic single-prompt studio", () => {
  // A ComfyUI install whose backend predates this UI would 404 on /targets. The
  // writer must degrade to the mode tabs and footer Generate, not half-render.
  const failure = main.match(/studio\.stage\.start\(\)\.catch\([\s\S]*?\n  \}\);/);
  assert.ok(failure, "stage startup failure is handled");
  assert.match(failure[0], /studio\.stage = null/);
  assert.match(failure[0], /syncWorkspace\(\)/);
  assert.match(failure[0], /showToast/);
});

test("media is session-wide and the mode tabs give way to the delivery bar", () => {
  assert.match(main, /const SESSION_MEDIA_MODE = "Reference";/);
  assert.match(main, /function mediaModeFor\(mode\)/);
  // Media is session-wide for every non-music mode, and an image mode must never
  // reach the video-only MODES table -- including with the stage nulled after a
  // failed start, when a persisted Krea 2 mode still comes back from preferences.
  assert.match(main, /mode !== "Music3" && \(studio\?\.stage \|\| !MODES\[mode\]\)\) return SESSION_MEDIA_MODE;/);
  // The Sequence workspace echoes the mode back when it is inactive, so only its
  // own `active` flag may decide that it owns the media zone.
  assert.match(main, /if \(studio\?\.sequence\?\.active\) return studio\.sequence\.mediaMode\(mode\);/);
  // An image mode must never reach the video-only MODES table, even with the
  // stage nulled after a failed start.
  assert.match(main, /studio\?\.stage \|\| !MODES\[mode\]/);
  assert.match(main, /\[data-video-modes\]"\).hidden = music \|\| Boolean\(studio.stage\)/);
  assert.match(main, /data-workspace="video">Writer</);
});

test("a compile refreshes the stored output, the audit badge and the goal verdicts", () => {
  assert.match(main, /studio\.stage\?\.afterCompile\(result, studio\.mode\)/);
  assert.match(main, /studio\.stage\?\.mediaChanged\(\)/);
  // Dropped references and the busy state both have to reach the stage.
  assert.match(main, /result\.media_warnings\?\.length/);
  assert.match(main, /studio\?\.stage\?\.syncBusy\(\)/);
});

test("the Settings editor shows the default composed with the flags in force", () => {
  assert.match(main, /getSystemPrompt\(requestMode, \{/);
  assert.match(main, /nsfw: session\?\.inputs\?\.nsfw !== false/);
  assert.match(main, /krea2: "Krea2"/);
  assert.match(main, /anima: "Anima"/);
});

test("the left rail persists server-side so a restart restores it", () => {
  assert.match(main, /studio\.stage\?\.setDuration\(studio\.durationSeconds\)/);
  assert.match(main, /studio\.stage\?\.setAspectRatio\(value\)/);
  assert.match(main, /studio\.stage\?\.setBrief\(videoBrief\.value\)/);
  assert.match(main, /videoBrief\.addEventListener\("blur", persistBrief\)/);
});

test("the session owns the brief and the prompt; the old drafts stand down", () => {
  // Two persistence layers for one field is how a reload came back with text
  // from a session you were not in.
  assert.match(main, /function draftsOwn\(mode\) \{/);
  assert.match(main, /!studio\?\.stage \|\| mode === "Music3"/);
  assert.match(main, /if \(!studio \|\| !draftsOwn\(studio\.mode\)\) return;/);
  assert.match(main, /if \(!draftsOwn\(mode\) && isPersistedDraftMode\(mode\)\)/);
});

test("a saved brief is restored into an empty box, never over live typing", () => {
  const handler = main.match(/onSessionChanged: \(session\) => \{[\s\S]*?\n    \},/);
  assert.ok(handler);
  assert.match(handler[0], /!brief\.value\.trim\(\)/);
  assert.doesNotMatch(handler[0], /activeElement/);
});

test("the stage colours come from theme tokens, not hardcoded hex", async () => {
  const css = await read("../web/styles/stage.css");
  assert.doesNotMatch(css, /:\s*#[0-9a-f]{3,8}\b/i);
  const shared = (await Promise.all(
    ["tokens", "foundation", "themes/dark", "themes/light"].map((name) => read(`../web/styles/${name}.css`)),
  )).join("\n");
  for (const [, token] of css.matchAll(/var\((--h3ps-[\w-]+)\)/g)) {
    assert.ok(shared.includes(`${token}:`), token);
  }
});
