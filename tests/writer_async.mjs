import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";
import test from "node:test";

const source = await readFile(process.env.H3_WRITER_SOURCE || new URL("../web/main.js", import.meta.url), "utf8");

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

// Execute the actual orchestration functions, replacing only their IO boundaries.
function controller(names, dependencies) {
  if (dependencies.studio) dependencies.studio.desktopNotifications = { notify() {} };
  const context = vm.createContext(dependencies);
  for (const name of names) {
    const declaration = source.match(new RegExp(`^(?:async )?function ${name}\\([^]*?^}`, "m"));
    assert.ok(declaration, name);
    vm.runInContext(declaration[0], context);
  }
  return context;
}

test("upload completion renders the current mode, including replacement", async () => {
  for (const replacement of [null, "old"]) {
    const pending = deferred();
    const renders = [];
    const studio = { mode: "Reference", assets: [], sessionId: "session" };
    const api = controller(["uploadFiles"], {
      studio, uploadMedia: () => pending.promise,
      showToast() {}, hideToast() {}, audioWasAdded: () => false,
      renderMedia: (mode) => renders.push(mode),
    });
    const operation = api.uploadFiles("Reference", [{}], replacement);
    studio.mode = "I2VA";
    pending.resolve({ session_id: "session", assets: [{ id: "new", mode: "Reference" }] });
    await operation;
    assert.deepEqual(renders, ["I2VA"]);
    assert.equal(studio.assets[0].mode, "Reference");
  }
});

test("Clear all clears the prompts before anything can be awaited", async () => {
  // The reported bug: it snapshotted the boxes, awaited the media clear, and
  // bailed if they had changed -- but the stage writes to those boxes during
  // that window (it refills an empty brief from the session and restores a
  // stored compiled prompt), so Clear all cleared the media and then said
  // "your current prompts were kept because the workspace changed".
  for (const changeMode of [false, true]) {
    const pending = deferred();
    const studio = { mode: "Reference" };
    let fields = { brief: "old", prompt: "old", lyrics: "" };
    const toasts = [];
    const api = controller(["clearEverything"], {
      studio, currentDraftFields: () => ({ ...fields }),
      clearCurrentMedia: () => pending.promise,
      clearCurrentPrompts: () => { fields = { brief: "", prompt: "" }; },
      showToast: (title) => toasts.push(title),
    });
    const operation = api.clearEverything();
    assert.deepEqual(fields, { brief: "", prompt: "" }, "cleared before the media request was awaited");
    // The stage repopulating the boxes mid-flight must not undo the clear.
    fields = { brief: "restored by a render", prompt: "restored by a render", lyrics: "" };
    // Clearing the media re-infers the mode by itself, so a mode change here is
    // the clear's own side effect and must not abandon the session reset.
    if (changeMode) studio.mode = "I2VA";
    pending.resolve(true);
    await operation;
    assert.match(toasts.join(" "), /Everything cleared/);
  }
});

test("a reload puts the session's media back, and a new upload still wins", async () => {
  for (const raced of [false, true]) {
    const pending = deferred();
    const studio = { sessionId: "s1", assets: [], mode: "Reference" };
    const renders = [];
    const api = controller(["restoreSessionMedia"], {
      studio,
      SESSION_MEDIA_MODE: "Reference",
      getMediaManifest: () => pending.promise,
      renderMedia: (mode) => renders.push(mode),
      syncModeAvailability() {},
    });
    const operation = api.restoreSessionMedia();
    // An upload that lands while the manifest is in flight is the newer truth.
    if (raced) studio.assets = [{ id: "just-uploaded" }];
    pending.resolve({ assets: [{ id: "stored" }] });
    await operation;
    assert.equal(studio.assets[0].id, raced ? "just-uploaded" : "stored");
    assert.deepEqual(renders, raced ? [] : ["Reference"]);
  }
});

test("Clear all resets the whole session instead of only the prompts", async () => {
  const scopes = [];
  // One request at a time, exactly like the stage: a second overlapping clear
  // would be dropped -- which is how Clear all used to leave the brief, the
  // goals and the conversation standing.
  let busy = false;
  const stage = { clear: async (options) => {
    if (busy) return false;
    busy = true;
    scopes.push(options.scope);
    await Promise.resolve();
    busy = false;
    return true;
  } };
  const fields = { brief: "old", prompt: "old", lyrics: "" };
  const api = controller(["clearEverything", "clearCurrentPrompts"], {
    studio: { mode: "Reference", stage, requestBusy: false, root: { querySelector: () => ({ value: "", textContent: "", hidden: false }) } },
    currentDraftFields: () => ({ ...fields }),
    clearPromptDraft: () => ({ brief: "", prompt: "" }),
    currentBriefTextarea: () => ({ value: "" }),
    clearCurrentMedia: async () => true,
    toggleRefine() {}, promptLengthMeta: () => "", updateBriefLayout() {}, renderPromptHighlights() {},
    syncModifiedState() {}, saveCurrentModeDraft() {}, showToast() {},
  });
  await api.clearEverything();
  assert.deepEqual(scopes, ["all"]);
});

function refinementController(lyricsMode, pending) {
  const started = deferred();
  const output = { value: "original" };
  const instruction = { value: "rewrite" };
  const generic = { value: "brief", checked: true, textContent: "", hidden: true };
  const submit = { disabled: false };
  const panel = { querySelector: (selector) => {
    if (selector.includes("submit")) return submit;
    if (selector === "textarea" || selector.includes("instruction")) return instruction;
    return generic;
  } };
  const studio = { mode: lyricsMode ? "Music3" : "Reference", selectedModel: { name: "model", runtime_ready: true }, root: {
    querySelector: (selector) => {
      if (selector.includes("panel")) return panel;
      if (selector === "[data-output]" || selector === "[data-music-lyrics]") return output;
      return generic;
    },
  } };
  const api = controller([lyricsMode ? "submitLyricsRefinement" : "submitRefinement"], {
    studio, prepareWriterRequest: async () => true, generationModeIsAvailable: () => true,
    markActiveWriterRequest() {}, clearActiveWriterRequest() {}, setGenerationState() {},
    // The busy button says what the click will do -- stop, or restart with the
    // inputs you just corrected.
    syncBusyGenerateLabel() {}, generationRestartWanted: () => false,
    vramHandoffCoordinator: { trackWriterRequest: (promise) => promise },
    refine: () => { started.resolve(); return pending.promise; },
    buildRefinePayload: () => ({}), buildLyricsRefinePayload: () => ({}),
    currentBriefTextarea: () => generic, newGenerationSeed: () => 1,
    renderPromptHighlights() {}, formatGenerationMeta: () => "", syncRuntimeSummary() {},
    syncModifiedState() {}, saveCurrentModeDraft() {}, showToast() {}, updateMusicLyricsCount() {},
    icon: () => "", getStatus: async () => ({}), updatePromptResidency() {},
  });
  return { api, output, instruction, started: started.promise };
}

for (const lyrics of [false, true]) {
  test(`${lyrics ? "Lyrics" : "Prompt"} Refine preserves newer editable text and instruction`, async () => {
    const pending = deferred();
    const { api, output, instruction, started } = refinementController(lyrics, pending);
    const operation = lyrics ? api.submitLyricsRefinement() : api.submitRefinement();
    await started;
    output.value = "new user text";
    instruction.value = "new instruction";
    pending.resolve({ prompt: "late result", total_seconds: 1, tokens_per_second: 1 });
    await operation;
    assert.equal(output.value, "new user text");
    assert.equal(instruction.value, "new instruction");
  });
}

test("Refine applies an owned result without clearing a newer instruction", async () => {
  const pending = deferred();
  const { api, output, instruction, started } = refinementController(false, pending);
  const operation = api.submitRefinement();
  await started;
  instruction.value = "next instruction";
  pending.resolve({ prompt: "rewritten", total_seconds: 1, tokens_per_second: 1 });
  await operation;
  assert.equal(output.value, "rewritten");
  assert.equal(instruction.value, "next instruction");
});

test("an in-flight status response cannot restore busy after generation finishes", async () => {
  const generation = deferred();
  const started = deferred();
  const status = deferred();
  const { api } = refinementController(false, generation);
  let poll;
  let statusCalls = 0;
  Object.assign(api, {
    generate: () => { started.resolve(); return generation.promise; },
    buildGeneratePayload: () => ({}),
    setInterval: (callback) => { poll = callback; return 1; },
    clearInterval() {},
    getStatus: () => ++statusCalls === 1 ? status.promise : Promise.resolve({}),
    setGenerationState: (phase) => { api.studio.requestBusy = phase === "busy"; },
  });
  controller(["startGenerationPreview"], api);
  const operation = api.startGenerationPreview();
  await started.promise;
  const oldPoll = poll();
  generation.resolve({ prompt: "done", total_seconds: 1, tokens_per_second: 1 });
  await operation;
  assert.equal(api.studio.requestBusy, false);
  status.resolve({ phase: "generating" });
  await oldPoll;
  assert.equal(api.studio.requestBusy, false);
});

const composerSource = await readFile(new URL("../web/media_composer.js", import.meta.url), "utf8");
test("an old Composer Add cannot clear a reopened composition", async () => {
  const upload = deferred();
  const started = deferred();
  const state = { items: [{}], open: true, openGeneration: 1 };
  const context = vm.createContext({
    state, dom: { add: { disabled: false }, dialog: { focus() {} } },
    exportPicture: async () => ({}),
    onAddPicture: () => { started.resolve(); return upload.promise; },
    normalizeCanvas() {}, clearDragGhost() {}, endCaption() {},
    notify() {}, renderPreview() {}, renderAll() {}, onOpenChange() {},
    setAssets: (assets) => { state.assets = assets; },
    document: { activeElement: null }, requestAnimationFrame: (callback) => callback(),
    root: { querySelector: () => null }, shellHomes: [],
    $: (selector) => selector === "[data-shell-controls]" ? { querySelector: () => ({}) } : null,
    el: { classList: { add() {}, remove() {} }, setAttribute() {} },
  });
  const declaration = composerSource.match(/^  async function addPicture\([^]*?^  }/m);
  vm.runInContext(declaration[0], context);
  vm.runInContext(composerSource.slice(composerSource.indexOf("  function open("), composerSource.indexOf("  function destroy(")), context);
  const operation = context.addPicture();
  await started.promise;
  context.close();
  context.open();
  state.items = [{ uid: "new composition" }];
  upload.resolve();
  await operation;
  assert.equal(state.items[0]?.uid, "new composition");
  assert.equal(state.open, true);
});

function providerController(overrides = {}) {
  const studio = {
    apiProviderConfig: { preset: "custom" }, apiProviderModels: [], models: [],
    root: { classList: { contains: () => false } }, modelSelectionRevision: 0,
    ...overrides,
  };
  return controller([
    "connectConfiguredApiProvider", "disconnectConfiguredApiProvider", "chooseApiProviderPreset",
    "refreshApiProviderModels", "connectExternalServer", "disconnectExternalServer", "refreshModels",
  ], {
    studio, localStorage: {}, showToast() {}, renderInferenceSettings() {}, syncRuntimeSummary() {},
    saveUserPreferences() {}, saveApiProviderConfig() {}, saveExternalServerConfig() {},
    apiProviderModelForSettings: () => null, disconnectApiProvider: async () => {},
    selectModel: (model) => { studio.selectedModel = model; studio.modelSelectionRevision++; },
    API_PROVIDER_UI: { custom: {}, openai: {} },
    getModels: async () => ({ models: [] }), getStatus: async () => ({}),
    getOllamaStatus: async () => ({}), getApiProviderPresets: async () => ({}),
    restoredModelAfterDiscovery: () => null, updatePromptResidency() {},
    setGenerationState: (phase) => { studio.requestBusy = phase === "busy"; },
    refreshGGUFRuntimeDiagnostics() {},
  });
}

function providerForm() {
  return { querySelector: () => ({}), elements: {
    model_id: { value: "model" }, url: { value: "http://localhost:8080" },
    model: { value: "model" }, api_key: { value: "" },
  } };
}

test("late API Connect releases only its own connection after a newer Connect", async () => {
  const first = deferred();
  const api = providerController();
  const disconnected = [];
  let calls = 0;
  api.disconnectApiProvider = async (id) => { disconnected.push(id); };
  api.probeApiProvider = () => ++calls === 1 ? first.promise : Promise.resolve({ connection: { id: "B" }, models: [] });
  const old = api.connectConfiguredApiProvider(providerForm());
  await api.connectConfiguredApiProvider(providerForm());
  first.resolve({ connection: { id: "A" }, models: [] });
  await old;
  assert.equal(api.studio.apiProviderConnection.id, "B");
  assert.deepEqual(disconnected, ["A"]);
});

test("a newer External Connect owns selection while an older API Connect finishes", async () => {
  const first = deferred();
  const second = deferred();
  const api = providerController();
  api.probeApiProvider = () => first.promise;
  api.probeExternalServer = () => second.promise;
  const old = api.connectConfiguredApiProvider(providerForm());
  const current = api.connectExternalServer(providerForm());
  first.resolve({ connection: { id: "A" }, model: { id: "A", name: "A" }, models: [] });
  await old;
  assert.equal(api.studio.selectedModel, undefined);
  second.resolve({ model: { id: "B", name: "B", endpoint: "http://localhost" } });
  await current;
  assert.equal(api.studio.selectedModel.id, "B");
});

test("a new Connect is not invalidated by an earlier background discovery", async () => {
  const discovery = deferred();
  const connection = deferred();
  const api = providerController();
  api.getModels = () => discovery.promise;
  api.probeApiProvider = () => connection.promise;
  const old = api.refreshModels();
  const current = api.connectConfiguredApiProvider(providerForm());
  discovery.resolve({ models: [] });
  await old;
  connection.resolve({ connection: { id: "new" }, model: { id: "new", name: "new" }, models: [] });
  await current;
  assert.equal(api.studio.selectedModel?.id, "new");
});

test("stale initial discovery finishes restoration and permits saving runtime preferences", async () => {
  const discovery = deferred();
  const connection = deferred();
  const api = providerController({ preferencesRestoring: true });
  controller(["rememberRuntimePreferences"], api);
  api.getModels = () => discovery.promise;
  api.probeApiProvider = () => connection.promise;
  const initial = api.refreshModels();
  const current = api.connectConfiguredApiProvider(providerForm());
  discovery.resolve({ models: [] });
  await initial;
  connection.resolve({ connection: { id: "new" }, model: { id: "new", name: "new" }, models: [] });
  await current;
  assert.equal(api.studio.selectedModel.id, "new");
  assert.equal(api.studio.preferencesRestoring, false);
  api.studio.contextProfile = "custom";
  api.studio.contextTokens = 24576;
  api.rememberRuntimePreferences("direct");
  assert.equal(api.studio.directContextTokens, 24576);
});

test("Disconnect invalidates pending API model refresh and Connect", async () => {
  for (const connect of [false, true]) {
    const pending = deferred();
    const api = providerController({ apiProviderConnection: { id: "old" } });
    api.getApiProviderModels = () => pending.promise;
    api.probeApiProvider = () => pending.promise;
    const operation = connect ? api.connectConfiguredApiProvider(providerForm()) : api.refreshApiProviderModels();
    await api.disconnectConfiguredApiProvider();
    pending.resolve({ connection: { id: "late" }, models: [] });
    await operation;
    assert.equal(api.studio.apiProviderConnection, null);
  }
});

test("changing API preset invalidates pending Connect without an existing connection", async () => {
  const pending = deferred();
  const api = providerController();
  api.probeApiProvider = () => pending.promise;
  const operation = api.connectConfiguredApiProvider(providerForm());
  await api.chooseApiProviderPreset("openai");
  pending.resolve({ connection: { id: "late" }, models: [] });
  await operation;
  assert.equal(api.studio.apiProviderConfig.preset, "openai");
});

test("External Disconnect invalidates a pending probe", async () => {
  const pending = deferred();
  const api = providerController();
  api.probeExternalServer = () => pending.promise;
  const operation = api.connectExternalServer(providerForm());
  api.disconnectExternalServer();
  pending.resolve({ model: { id: "late", endpoint: "http://localhost", name: "late" } });
  await operation;
  assert.equal(api.studio.externalModel, null);
});

test("model discovery cannot release a generation started while it waited", async () => {
  const pending = deferred();
  const api = providerController();
  api.getModels = () => pending.promise;
  const operation = api.refreshModels();
  api.studio.requestBusy = true;
  pending.resolve({ models: [] });
  await operation;
  assert.equal(api.studio.requestBusy, true);
});

test("discovery probe cannot restore the model selected before a newer selection", async () => {
  const pending = deferred();
  const started = deferred();
  const oldModel = { id: "old" };
  const newModel = { id: "new" };
  const api = providerController({ selectedModel: oldModel, externalServerConfig: {} });
  api.getModels = async () => ({ models: [oldModel, newModel] });
  api.probeExternalServer = () => { started.resolve(); return pending.promise; };
  const operation = api.refreshModels();
  await started.promise;
  api.selectModel(newModel);
  pending.resolve({ model: { id: "external" } });
  await operation;
  assert.equal(api.studio.selectedModel.id, "new");
});

test("a second click with edited inputs restarts instead of only stopping", async () => {
  // The reported flow: press Generate, notice the duration is wrong, fix it,
  // press again. The run in flight is abandoned and a fresh one takes its place.
  const cancels = [];
  const api = controller(["startGenerationPreview"], {
    studio: { requestBusy: true, activeRequestFingerprint: "duration=5" },
    generationRestartWanted: () => true,
    setGenerationState() {},
    cancel: async () => { cancels.push("cancelled"); },
    generate: () => assert.fail("the new run must wait for this one to unwind"),
  });
  await api.startGenerationPreview();
  assert.deepEqual(cancels, ["cancelled"]);
  assert.equal(api.studio.restartAfterCancel, true, "the restart is armed for the finally block");
});

test("a second click with untouched inputs still just stops", async () => {
  const api = controller(["startGenerationPreview"], {
    studio: { requestBusy: true, activeRequestFingerprint: "duration=5" },
    generationRestartWanted: () => false,
    setGenerationState() {},
    cancel: async () => {},
  });
  await api.startGenerationPreview();
  assert.equal(api.studio.restartAfterCancel, false, "stop must stay reachable; it is the only control that stops a run");
});

test("a restart is wanted only when the request itself changed", () => {
  const api = controller(["generationRestartWanted"], {
    studio: { requestBusy: true, activeRequestFingerprint: "duration=5" },
    generationRequestFingerprint: () => "duration=10",
  });
  assert.equal(api.generationRestartWanted(), true);

  const same = controller(["generationRestartWanted"], {
    studio: { requestBusy: true, activeRequestFingerprint: "duration=5" },
    generationRequestFingerprint: () => "duration=5",
  });
  assert.equal(same.generationRestartWanted(), false, "a new seed alone is not an edit");

  // Refinement never fingerprints its request, so its runs only ever stop.
  const refining = controller(["generationRestartWanted"], {
    studio: { requestBusy: true, activeRequestFingerprint: null },
    generationRequestFingerprint: () => "anything",
  });
  assert.equal(refining.generationRestartWanted(), false);
});

test("the busy button says which of the two things the click will do", () => {
  for (const [restart, expected] of [[true, "Generating, click to restart"], [false, "Generating, click to stop"]]) {
    const label = { textContent: "" };
    const api = controller(["syncBusyGenerateLabel"], {
      studio: { requestBusy: true, root: { querySelector: (s) => s === "[data-generate] [data-generate-label]" ? label : null } },
      generationRestartWanted: () => restart,
    });
    api.syncBusyGenerateLabel();
    assert.equal(label.textContent, expected);
  }
});
