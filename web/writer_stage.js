/**
 * The two-pane writer stage.
 *
 * Left: what you have (media, duration, aspect, Naughty, Story builder), the
 * brief, Generate, and an ongoing conversation.
 * Right: the generic prompt it produced, the standing goals, then the model
 * picker and its own Generate.
 *
 * The generic prompt is the source of truth. Everything on the right is compiled
 * from it, so switching from H3 to Krea 2 cannot lose a fact -- and the
 * conversation edits structured fields, never the compiled prose, because
 * editing prose makes the model anchor on it and swap surfaces instead of
 * rethinking the scene (see docs/PLAN_IMAGINATION_AND_ASSETS.md).
 *
 * It is injected into the existing studio DOM rather than replacing it, so the
 * header, Settings, media zone, model picker, status line and the Sequence
 * workspace all keep working untouched.
 */
import { inferH3Mode } from "./mode_inference.js";
import {
  buildGeneric,
  changeGoal,
  describeAsset,
  editGenericField,
  renamePerson,
  expandBrief,
  getSession,
  getTargets,
  resetSession,
  saveInputs,
  sendGenericTurn,
} from "./api/h3studio.js";

const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[character]));

const ORIGIN_CHIPS = {
  asset: { label: "from image", hint: "Read from your reference media. Locked into every prompt." },
  user: { label: "your words", hint: "You said it. Locked into every prompt." },
  override: { label: "your choice", hint: "Your deliberate change from what the image shows. Never reverted." },
  invented: { label: "invented", hint: "Filled in by Story builder. Replaced freely." },
  unspecified: { label: "not specified", hint: "Nothing has fixed this yet, so a model may invent it." },
};
const VERDICT_MARKS = { met: "✓", unmet: "✗", pending: "…" };
// What each goal kind promises, because "judged" and "presence" mean nothing on
// their own and the difference decides how much the verdict is worth.
const GOAL_KIND_HINTS = {
  field: "checked against the generic prompt's own fields",
  presence: "checked by looking for it in the compiled prompt",
  judged: "checked by a verifier pass over the finished prompt",
};
const AUTO_PREFIX = "auto:";
// Mirrors backend/heat.py. The dial replaced a boolean that had to mean four
// different things at once.
const HEAT_LABELS = ["Clean", "Allowed", "Sensual", "Explicit", "Graphic"];
const HEAT_HINTS = [
  "Nothing sexual, and nothing suggestive.",
  "Adult content only where you ask for it.",
  "Draws out the charge in a scene: bodies, contact, wardrobe.",
  "Names nudity, anatomy and acts plainly.",
  "Pornographic prose, crude and direct.",
];
const DEFAULT_HEAT = 2;

function heatLevel(inputs) {
  const stored = inputs?.heat;
  if (Number.isInteger(stored)) return Math.max(0, Math.min(4, stored));
  // A session that predates the dial carries only the boolean.
  return inputs?.nsfw === false ? 0 : DEFAULT_HEAT;
}

/** Which person a document group belongs to, if any. Subject fields carry the
 *  suffix, so the group's own first field says who it is about. */
function personIndexOf(group) {
  const key = (group.fields || [])[0] || "";
  if (!key.startsWith("subject")) return 0;
  const [, suffix] = key.split("#");
  return suffix ? Number(suffix) : 1;
}

export function createWriterStage(host) {
  const { root, icon } = host;
  let registry = null;
  let session = null;
  let busy = false;
  let editing = null;
  // Which target (if any) is following the attached media rather than a mode the
  // user pinned. Held here, not persisted: pinning a mode is the exception.
  let autoTargetId = null;
  // The wording before the last expansion, for a single undo.
  let previousBrief = null;

  const query = (selector) => root.querySelector(selector);

  // ---------------------------------------------------------------- markup

  function mount() {
    const inputs = query("[data-video-inputs]");
    const authoring = root.ownerDocument.createElement("section");
    authoring.className = "h3ps-author-panel";
    authoring.dataset.authorPanel = "";
    query(".h3ps-output-panel").before(authoring);
    const left = document.createElement("div");
    left.className = "h3ps-stage-left";
    left.innerHTML = `
      <div class="h3ps-stage-flags" role="group" aria-label="Writing behaviour">
        <label class="h3ps-heat" title="How far the writer goes, from clean to graphic. It applies when you expand a brief, when the document is built, and when a prompt is compiled.">
          <span class="h3ps-heat-head">Naughty <b data-heat-label>Sensual</b></span>
          <input type="range" min="0" max="4" step="1" value="2" data-heat aria-label="Naughty level">
        </label>
        <label class="h3ps-toggle-control" title="Invent supporting detail the brief leaves open, instead of staying literal">
          <input type="checkbox" data-stage-flag="story"><span></span>Story builder
        </label>
      </div>
      <p class="h3ps-stage-flag-effect" data-flag-effect></p>
      <div class="h3ps-stage-actions">
        <button class="h3ps-primary-button h3ps-stage-build" type="button" data-stage-build>
          ${icon("spark", 15)}<span data-stage-build-label>Generate generic prompt</span>
        </button>
        <button class="h3ps-text-button h3ps-stage-reset" type="button" data-stage-reset
                title="Clear the generic prompt, goals, conversation and compiled prompts">Reset</button>
      </div>
      <section class="h3ps-stage-chat" aria-label="Conversation">
        <header>
          <span><strong>Conversation</strong><small data-stage-chat-hint>Steer the generic prompt by talking. It keeps until you reset.</small></span>
        </header>
        <div class="h3ps-stage-log" data-stage-log role="log" aria-live="polite"></div>
        <div class="h3ps-stage-composer">
          <textarea rows="2" spellcheck="true" data-stage-message
                    placeholder="e.g. her skirt is red, not blue — or: always follow the clothing colours in the image"></textarea>
          <button class="h3ps-secondary-button" type="button" data-stage-send>${icon("spark", 14)} Send</button>
        </div>
      </section>`;
    // The brief seeds the document and the conversation steers it, so they
    // belong together in the middle column; the left column stays references.
    // Order: flags, brief, Generate, conversation.
    authoring.append(left);
    const brief = inputs.querySelector(".h3ps-brief");
    if (brief) {
      const tools = root.ownerDocument.createElement("div");
      tools.className = "h3ps-brief-tools";
      tools.innerHTML = `
        <button class="h3ps-text-button" type="button" data-brief-expand
                title="Rewrite your brief as a fuller one, in your words. You can read it, edit it or undo it.">
          Expand brief
        </button>
        <button class="h3ps-text-button" type="button" data-brief-undo hidden>Undo expand</button>`;
      const anchor = left.querySelector(".h3ps-stage-actions");
      anchor.before(brief);
      anchor.before(tools);
    }

    const output = query(".h3ps-output-panel");
    const right = document.createElement("div");
    right.className = "h3ps-stage-right";
    right.innerHTML = `
      <section class="h3ps-generic-card" aria-label="Generic prompt">
        <header>
          <span><strong>Generic prompt</strong><small data-generic-summary>Not built yet</small></span>
          <span class="h3ps-generic-actions">
            <button class="h3ps-icon-button h3ps-field-copy" type="button" data-generic-copy
                    title="Copy the whole document as text" aria-label="Copy the generic prompt">${icon("copy", 14)}</button>
            <button class="h3ps-text-button" type="button" data-generic-toggle aria-expanded="true">Hide</button>
          </span>
        </header>
        <p class="h3ps-generic-empty" data-generic-empty>
          Add a brief or a reference image on the left, then press <strong>Generate generic prompt</strong>.
          It is model-agnostic: every model on the right is compiled from it.
        </p>
        <div class="h3ps-generic-body" data-generic-body hidden>
          <div class="h3ps-generic-groups" data-generic-groups></div>
          <section class="h3ps-goal-ledger" data-goal-ledger>
            <header>
              <span><strong>Goals</strong><small>Applied to every prompt, and checked after each one</small></span>
            </header>
            <ul data-goal-list></ul>
            <div class="h3ps-goal-add">
              <input type="text" data-goal-text placeholder="Add a standing goal, e.g. never mention a brand name">
              <button class="h3ps-text-button" type="button" data-goal-add>Add</button>
            </div>
          </section>
        </div>
        <p class="h3ps-generic-warning" data-generic-media-warning hidden></p>
      </section>
      <div class="h3ps-delivery-bar" data-delivery-bar>
        <span class="h3ps-delivery-controls-slot" data-delivery-controls-slot></span>
        <span class="h3ps-delivery-model">
          <label for="h3ps-delivery-target">Model</label>
          <select id="h3ps-delivery-target" data-delivery-target></select>
        </span>
        <span class="h3ps-delivery-variant" data-delivery-variant hidden>
          <label for="h3ps-delivery-variant-select">Variant</label>
          <select id="h3ps-delivery-variant-select" data-delivery-variant-select></select>
        </span>
        <span class="h3ps-delivery-mode" data-delivery-mode></span>
        <span class="h3ps-delivery-repairs" title="How many times the writer may correct its own draft before it hands the problem back to you">
          <label for="h3ps-repair-attempts">Retries</label>
          <select id="h3ps-repair-attempts" data-repair-attempts></select>
        </span>
        <button class="h3ps-primary-button" type="button" data-stage-compile>
          ${icon("spark", 15)}<span data-stage-compile-label>Generate prompt</span>
        </button>
      </div>`;
    output.prepend(right);

    const negative = document.createElement("section");
    negative.className = "h3ps-negative-output";
    negative.dataset.negativeOutput = "";
    negative.hidden = true;
    negative.innerHTML = `
      <header><span><strong>Negative prompt</strong><small data-negative-note></small></span>
        <button class="h3ps-icon-button h3ps-field-copy" type="button" data-negative-copy
                title="Copy the negative prompt" aria-label="Copy the negative prompt">${icon("copy", 14)}</button></header>
      <textarea class="h3ps-editor h3ps-negative-editor" rows="2" spellcheck="false" data-negative-value aria-label="Negative prompt"></textarea>`;
    query(".h3ps-editor-wrap").after(negative);

    // Duration and aspect ratio are delivery parameters, not scene facts: the
    // same document should compile to a five-second clip or a fifteen-second
    // one. Moved rather than rebuilt, so their existing bindings survive -- and
    // if the stage never starts they stay where they were.
    const controls = query("[data-delivery-controls]");
    if (controls) query("[data-delivery-controls-slot]").append(controls);

    const audit = document.createElement("p");
    audit.className = "h3ps-goal-summary";
    audit.dataset.goalSummary = "";
    audit.hidden = true;
    query(".h3ps-output-actions").before(audit);
    bind();
  }

  // ----------------------------------------------------------- rendering

  function fieldRow(key) {
    const record = session.fields[key] || { value: "", origin: "unspecified" };
    const chip = ORIGIN_CHIPS[record.origin] || ORIGIN_CHIPS.unspecified;
    const value = record.value
      ? escape(record.value)
      : '<em class="h3ps-generic-blank">not specified</em>';
    const observed = record.observed && record.origin === "override"
      ? `<small class="h3ps-generic-observed">image shows: ${escape(record.observed)}</small>`
      : "";
    return `
      <div class="h3ps-generic-row" data-field="${escape(key)}" data-origin="${escape(record.origin)}">
        <span class="h3ps-generic-label">${escape(session.labels[key] || key)}</span>
        <span class="h3ps-generic-value" data-generic-value tabindex="0" role="button"
              title="Click to edit. Your edit becomes your words and is locked in.">${value}${observed}</span>
        <span class="h3ps-origin-chip" data-origin-chip title="${escape(chip.hint)}">${escape(chip.label)}</span>
      </div>`;
  }

  function renderDocument() {
    const groups = query("[data-generic-groups]");
    groups.innerHTML = session.groups.map((group) => {
      // A person's name is a handle the user owns: one click to change it,
      // because a name read off a picture can be the wrong one and it sticks.
      const person = personIndexOf(group);
      const heading = person
        ? `<h4><button type="button" class="h3ps-person-name" data-rename-person="${person}"
             title="Rename this person">${escape(group.title)}</button></h4>`
        : `<h4>${escape(group.title)}</h4>`;
      return `
      <section class="h3ps-generic-group">
        ${heading}
        ${group.fields.map(fieldRow).join("")}
      </section>`;
    }).join("");
  }

  /** The document as pasteable text: the fields that have a value, grouped and
   *  labelled the way the card shows them. */
  function documentText() {
    return (session?.groups || []).map((group) => {
      const lines = group.fields
        .map((key) => [session.labels[key] || key, session.fields[key]?.value])
        .filter(([, value]) => value)
        .map(([label, value]) => `${label}: ${value}`);
      return lines.length ? `${group.title}\n${lines.join("\n")}` : "";
    }).filter(Boolean).join("\n\n");
  }

  function renderGoals() {
    const list = query("[data-goal-list]");
    const goals = session.goals || [];
    if (!goals.length) {
      list.innerHTML = `<li class="h3ps-goal-empty">No goals yet. Say "always follow the clothing colours in the image" in the conversation, or add one here.</li>`;
      return;
    }
    list.innerHTML = goals.map((goal) => `
      <li class="h3ps-goal" data-goal-id="${escape(goal.id)}" data-verdict="${escape(goal.verdict)}"
          data-enabled="${goal.enabled ? "true" : "false"}">
        <span class="h3ps-goal-mark" title="${escape(goal.reason || "")}">${VERDICT_MARKS[goal.verdict] || "…"}</span>
        <span class="h3ps-goal-text">${escape(goal.text)}
          ${goal.reason ? `<small>${escape(goal.reason)}</small>` : ""}</span>
        <span class="h3ps-goal-kind" title="${escape(GOAL_KIND_HINTS[goal.kind] || "")}">${escape(goal.kind)}</span>
        <button class="h3ps-text-button" type="button" data-goal-toggle
                title="${goal.enabled ? "Pause this goal" : "Resume this goal"}">${goal.enabled ? "Pause" : "Resume"}</button>
        <button class="h3ps-text-button" type="button" data-goal-delete title="Delete this goal">Delete</button>
      </li>`).join("");
  }

  function renderConversation() {
    const log = query("[data-stage-log]");
    const turns = session.conversation || [];
    if (!turns.length) {
      log.innerHTML = `<p class="h3ps-stage-log-empty">Nothing yet. After the first Generate, say what should change.</p>`;
      return;
    }
    log.innerHTML = turns.map((turn) => {
      const changed = (turn.changed || []).map((key) => session.labels[key] || key);
      const protectedFields = (turn.protected || []).map((key) => session.labels[key] || key);
      const notes = [
        changed.length ? `updated ${changed.join(", ")}` : "",
        protectedFields.length ? `left your ${protectedFields.join(", ")} alone` : "",
        (turn.goals_added || []).length ? `new goal: ${turn.goals_added.join("; ")}` : "",
      ].filter(Boolean);
      return `
        <div class="h3ps-stage-turn" data-role="${escape(turn.role)}">
          <span class="h3ps-stage-turn-text">${escape(turn.text)}</span>
          ${notes.length ? `<small class="h3ps-stage-turn-notes">${escape(notes.join(" · "))}</small>` : ""}
        </div>`;
    }).join("");
    log.scrollTop = log.scrollHeight;
  }

  function renderSummary() {
    const total = session.field_order.length;
    const origins = session.field_order.map((key) => session.fields[key]?.origin);
    const filled = session.field_order.filter((key) => session.fields[key]?.value).length;
    const locked = origins.filter((origin) => ["asset", "user", "override"].includes(origin)).length;
    const invented = origins.filter((origin) => origin === "invented").length;
    // Counting the invented and unspecified fields is how Story builder becomes
    // legible after the fact: off, the second number grows instead of the first.
    const summary = filled
      ? [
          `${filled} of ${total} fields`,
          `${locked} locked into every prompt`,
          invented ? `${invented} invented` : "",
          total - filled ? `${total - filled} not specified` : "",
        ].filter(Boolean).join(" · ")
      : "Not built yet";
    query("[data-generic-summary]").textContent = summary;
    const empty = !filled;
    query("[data-generic-empty]").hidden = !empty;
    query("[data-generic-body]").hidden = empty;
    const missing = session.media_missing || [];
    const warning = query("[data-generic-media-warning]");
    warning.hidden = missing.length === 0;
    if (missing.length) {
      warning.textContent = `${missing.map((item) => item.filename).join(", ")} `
        + "is no longer loaded, so the picture cannot be re-read. Add it again to correct anything from the image.";
    }
  }

  function targetsForStage() {
    return (registry?.targets || []).filter((target) => target.workspace !== "music");
  }

  function renderDelivery() {
    const select = query("[data-delivery-target]");
    const current = host.currentMode();
    const auto = autoTarget();
    select.innerHTML = targetsForStage().map((target) => {
      const modes = target.modes.filter((mode) => mode.generation);
      const options = [];
      if (target.infers_mode) {
        // Pick the model, not the jargon: "I2VA" means nothing to someone who
        // just wants a video. The explicit modes stay available as overrides.
        const inferred = inferH3Mode(host.assets());
        const summary = modes.find((mode) => mode.id === inferred)?.summary || "";
        const selected = auto === target.id ? " selected" : "";
        options.push(`<option value="${AUTO_PREFIX}${escape(target.id)}"${selected}>Automatic — ${escape(summary)}</option>`);
      }
      for (const mode of modes) {
        const selected = auto !== target.id && mode.id === current ? " selected" : "";
        options.push(`<option value="${escape(mode.id)}"${selected}>${escape(mode.label)}</option>`);
      }
      return modes.length > 1 || target.infers_mode
        ? `<optgroup label="${escape(target.label)}">${options.join("")}</optgroup>`
        : options.join("");
    }).join("");

    const target = targetFor(current);
    const variantWrap = query("[data-delivery-variant]");
    variantWrap.hidden = !target?.variants?.length;
    if (target?.variants?.length) {
      const chosen = session?.target?.variant || target.default_variant;
      query("[data-delivery-variant-select]").innerHTML = target.variants
        .map((variant) => `<option value="${escape(variant)}" ${variant === chosen ? "selected" : ""}>${escape(variant)}</option>`)
        .join("");
    }
    // H3 counts shots against the duration; a still has no runtime at all.
    const durationField = root.querySelector("[data-duration-field]");
    if (durationField) durationField.hidden = !target?.fields?.includes("duration_seconds");
    const aspectField = root.querySelector('[data-delivery-controls] [data-choice-toggle="aspect"]')?.closest(".h3ps-choice");
    if (aspectField) aspectField.hidden = !target?.fields?.includes("aspect_ratio");

    const inferred = host.inferredModeSummary();
    const stale = outputIsStale(current);
    // Say it once, where the Generate button is: a prompt compiled before the
    // last conversation turn is not what the document says any more.
    query("[data-delivery-mode]").textContent = stale
      ? "— compiled before your last change"
      : inferred ? `— ${inferred}` : "";
    query("[data-delivery-mode]").dataset.stale = stale ? "true" : "false";
    query("[data-stage-compile-label]").textContent = target ? `Generate ${target.label} prompt` : "Generate prompt";
  }

  function renderFlags() {
    root.querySelectorAll("[data-stage-flag]").forEach((input) => {
      input.checked = session.inputs?.[input.dataset.stageFlag] !== false;
    });
    const level = heatLevel(session.inputs);
    const slider = query("[data-heat]");
    slider.value = String(level);
    slider.style.setProperty("--h3ps-range", `${level / 4 * 100}%`);
    query("[data-heat-label]").textContent = HEAT_LABELS[level];
    // Say what the switches will do, before a generation proves it. They edit a
    // system prompt nobody reads, so without this they are invisible until the
    // output lands -- and by then it is too late to have chosen differently.
    const story = session.inputs?.story !== false;
    query("[data-flag-effect]").textContent = [
      story
        ? "Gaps get invented detail you can replace."
        : "Only what you or your references supply; gaps stay unspecified.",
      HEAT_HINTS[heatLevel(session.inputs)],
    ].join(" ");
  }

  function render() {
    if (!session || !registry) return;
    renderFlags();
    renderDocument();
    renderGoals();
    renderConversation();
    renderSummary();
    renderDelivery();
    renderOutputFor(host.currentMode());
    syncBusy();
  }

  function autoTarget() {
    return autoTargetId;
  }

  function applyAuto() {
    if (!autoTargetId) return;
    const target = (registry?.targets || []).find((item) => item.id === autoTargetId);
    if (target?.infers_mode) host.selectMode(inferH3Mode(host.assets()), { silent: true });
  }

  function targetFor(mode) {
    return (registry?.targets || []).find((target) => target.modes.some((item) => item.id === mode));
  }

  /** Whether this target's stored prompt predates the document it came from. */
  function outputIsStale(mode) {
    const stored = session?.outputs?.[mode];
    const current = session?.generic?.updated_at;
    return Boolean(stored && current && stored.generic_updated_at && stored.generic_updated_at !== current);
  }

  /** Show the last compiled prompt for this target instead of blanking the pane. */
  function renderOutputFor(mode) {
    const stored = session?.outputs?.[mode];
    const target = targetFor(mode);
    const negative = query("[data-negative-output]");
    negative.hidden = !target?.fields?.includes("negative_prompt");
    if (stored) {
      const output = query("[data-output]");
      if (host.outputIsReplaceable(stored.prompt)) {
        output.value = stored.prompt || "";
        host.afterOutputReplaced(stored);
      }
      query("[data-negative-value]").value = stored.negative_prompt || "";
      query("[data-negative-note]").textContent = outputIsStale(mode) ? "from an earlier generic prompt" : "";
      renderGoalSummary(stored.audit);
    } else {
      query("[data-negative-value]").value = "";
      renderGoalSummary(null);
    }
  }

  function renderGoalSummary(audit) {
    const element = query("[data-goal-summary]");
    const unmet = (audit?.goals || []).filter((goal) => goal.enabled && goal.verdict === "unmet");
    const pending = (audit?.goals || []).filter((goal) => goal.enabled && goal.verdict === "pending");
    if (!audit || (!unmet.length && !pending.length)) {
      element.hidden = true;
      return;
    }
    element.hidden = false;
    element.dataset.state = unmet.length ? "unmet" : "pending";
    element.textContent = unmet.length
      ? `${unmet.length} goal${unmet.length > 1 ? "s" : ""} unmet: ${unmet.map((goal) => goal.text).join("; ")}`
      : `${pending.length} goal${pending.length > 1 ? "s" : ""} could not be verified.`;
  }

  function syncBusy() {
    const disabled = busy || host.requestBusy();
    root.querySelectorAll("[data-stage-build], [data-stage-send], [data-stage-compile], [data-stage-reset], [data-brief-expand], [data-brief-undo]")
      .forEach((button) => { button.disabled = disabled; });
  }

  // ------------------------------------------------------------- actions

  async function withBusy(label, work) {
    if (busy) return;
    busy = true;
    syncBusy();
    host.setStatus(label);
    try {
      return await work();
    } catch (error) {
      host.notifyError(error);
      return null;
    } finally {
      busy = false;
      host.clearStatus();
      syncBusy();
    }
  }

  function applySession(payload) {
    if (payload?.session) {
      session = payload.session;
      host.onSessionChanged(session);
      render();
    }
    return payload;
  }

  async function persistInputs(patch) {
    if (!session) return;
    session = { ...session, inputs: { ...session.inputs, ...patch } };
    applySession(await saveInputs({ session_id: host.sessionId(), ...patch }));
    // The Settings panel shows the contract composed with these flags, cached.
    if ("nsfw" in patch || "story" in patch || "variant" in patch) host.invalidateSystemPrompts?.();
  }

  async function build() {
    await withBusy("Building the generic prompt", async () => {
      const payload = await buildGeneric({
        ...host.inferencePayload(),
        session_id: host.sessionId(),
        brief: host.briefValue(),
      });
      applySession(payload);
      const changed = (payload.changed || []).length;
      host.notify("Generic prompt ready", changed
        ? `${changed} field${changed > 1 ? "s" : ""} set. Steer it in the conversation, then pick a model on the right.`
        : "No fields changed.");
    });
  }

  async function send() {
    const box = query("[data-stage-message]");
    const message = box.value.trim();
    if (!message) return;
    await withBusy("Thinking", async () => {
      const payload = await sendGenericTurn({
        ...host.inferencePayload(),
        session_id: host.sessionId(),
        message,
      });
      box.value = "";
      applySession(payload);
      if ((payload.protected || []).length) {
        const labels = payload.protected.map((key) => session.labels[key] || key).join(", ");
        host.notify("Kept your own values", `${labels} stayed as you set them. Say it directly to change one.`);
      }
    });
  }

  /** Rewrite the brief in place, keeping the previous wording for one undo. */
  async function expand() {
    const box = host.briefElement?.();
    const before = host.briefValue().trim();
    if (!before) {
      host.notify("Nothing to expand", "Write a line or two first, then expand it.");
      return;
    }
    await withBusy("Expanding the brief", async () => {
      const payload = await expandBrief({ ...host.inferencePayload(), session_id: host.sessionId(), brief: before });
      applySession(payload);
      if (box && payload.brief) box.value = payload.brief;
      offerUndo(payload.previous_brief ?? before, "Undo expand");
      // Naming the level makes the dial's effect checkable: "it did nothing"
      // and "it did something I did not notice" look identical otherwise.
      host.notify(
        `Brief expanded at ${payload.heat_label || HEAT_LABELS[heatLevel(session?.inputs)]}`,
        "Read it over and edit anything you disagree with, or undo it.",
      );
    });
  }

  /** Arm the single brief undo, saying which rewrite it would take back. */
  function offerUndo(wording, label) {
    previousBrief = wording;
    const button = query("[data-brief-undo]");
    button.textContent = label;
    button.hidden = false;
  }

  /** Write the brief from ONE attached image -- the studio's double-click.
   *
   *  Appends when the brief already says something, so describing a second
   *  picture (or double-clicking with notes already typed) adds to the brief
   *  instead of replacing it. Either way the undo puts the old wording back. */
  async function describe(assetId) {
    const asset = (host.assets() || []).find((item) => item.id === assetId);
    if (!asset) return false;
    if (asset.type !== "image") {
      host.notify("Images only", "Only a picture can be described into the brief.");
      return false;
    }
    const box = host.briefElement?.();
    const before = host.briefValue();
    let done = false;
    await withBusy(`Reading ${asset.reference || asset.filename}`, async () => {
      // The brief box debounces its save, so what is on screen may not be in the
      // session yet -- and the description would be appended to a stale brief.
      if (before.trim() !== String(session?.inputs?.brief || "").trim()) {
        applySession(await saveInputs({ session_id: host.sessionId(), brief: before }));
      }
      const payload = await describeAsset({
        ...host.inferencePayload(), session_id: host.sessionId(), asset_id: assetId,
      });
      applySession(payload);
      if (box && payload.brief) box.value = payload.brief;
      offerUndo(payload.previous_brief ?? before, "Undo description");
      done = true;
      host.notify(
        before.trim() ? "Added to the brief" : "Brief written from the image",
        "Read it over and edit anything you disagree with, or undo it.",
      );
    });
    return done;
  }

  async function undoExpand() {
    const box = host.briefElement?.();
    if (previousBrief === null) return;
    if (box) box.value = previousBrief;
    await withBusy("Restoring", async () => {
      applySession(await saveInputs({ session_id: host.sessionId(), brief: previousBrief }));
    });
    previousBrief = null;
    query("[data-brief-undo]").hidden = true;
  }

  async function reset({ scope = "all", clearMedia = true, confirm = true, notify = true } = {}) {
    if (confirm && !host.confirmReset()) return false;
    let done = false;
    await withBusy(scope === "prompts" ? "Clearing prompts" : "Resetting", async () => {
      // Emptied before the session lands, so `onSessionChanged` sees an empty box
      // and has nothing to restore into it.
      const brief = host.briefElement?.();
      if (brief) brief.value = "";
      applySession(await resetSession({ session_id: host.sessionId(), scope, clear_media: clearMedia }));
      if (scope === "all") {
        host.afterReset();
        // A full reset means defaults, not just empty text: the fresh session
        // carries duration, ratio and both flags, and the studio holds its own
        // copies that would otherwise be written straight back.
        host.applyInputs?.(session?.inputs);
        autoTargetId = null;
        const inferring = targetsForStage().find((target) => target.infers_mode);
        if (inferring) {
          autoTargetId = inferring.id;
          applyAuto();
        }
        render();
      }
      done = true;
      if (!notify) return;
      host.notify(
        scope === "prompts" ? "Prompts cleared" : "Session reset",
        scope === "prompts"
          ? "The brief, the generic prompt, the goals, the conversation and every compiled prompt were cleared. Your media was kept."
          : "The generic prompt, goals, conversation and compiled prompts were cleared.",
      );
    });
    return done;
  }

  function startRename(button) {
    if (editing || busy) return;
    const person = Number(button.dataset.renamePerson);
    const current = (session.names || {})[String(person)] || "";
    editing = `name:${person}`;
    button.outerHTML = `<input type="text" class="h3ps-generic-input h3ps-person-input" value="${escape(current)}"
      maxlength="40" aria-label="Name for this person">`;
    const input = query(".h3ps-person-input");
    input.focus();
    input.select();
    const commit = async () => {
      const next = input.value.trim();
      editing = null;
      if (next === current) { render(); return; }
      await withBusy("Renaming", async () => {
        applySession(await renamePerson({ session_id: host.sessionId(), person, name: next }));
      });
      render();
    };
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); commit(); }
      if (event.key === "Escape") { event.preventDefault(); editing = null; render(); }
    });
    input.addEventListener("blur", commit, { once: true });
  }

  function startEdit(row) {
    if (editing || busy) return;
    const key = row.dataset.field;
    const value = session.fields[key]?.value || "";
    const holder = row.querySelector("[data-generic-value]");
    editing = key;
    holder.innerHTML = `<input type="text" class="h3ps-generic-input" value="${escape(value)}"
      aria-label="${escape(session.labels[key] || key)}">`;
    const input = holder.querySelector("input");
    input.focus();
    input.select();
    const commit = async () => {
      const next = input.value.trim();
      editing = null;
      if (next === value) { render(); return; }
      await withBusy("Saving", async () => {
        applySession(await editGenericField({ session_id: host.sessionId(), field: key, value: next }));
      });
      render();
    };
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); commit(); }
      if (event.key === "Escape") { event.preventDefault(); editing = null; render(); }
    });
    input.addEventListener("blur", commit, { once: true });
  }

  async function goalAction(body) {
    await withBusy("Updating goals", async () => {
      applySession(await changeGoal({ session_id: host.sessionId(), ...body }));
    });
  }

  function bind() {
    root.querySelectorAll("[data-stage-flag]").forEach((input) => {
      input.addEventListener("change", () => persistInputs({ [input.dataset.stageFlag]: input.checked }));
    });
    const heat = query("[data-heat]");
    // Label and fill follow the thumb immediately; the session is told when the
    // user lets go, so dragging across the range is not five round trips.
    heat.addEventListener("input", () => {
      const level = Number(heat.value);
      heat.style.setProperty("--h3ps-range", `${level / 4 * 100}%`);
      query("[data-heat-label]").textContent = HEAT_LABELS[level];
      query("[data-flag-effect]").textContent = query("[data-flag-effect]").textContent
        .replace(/(?:[^.]*\.)$/, ` ${HEAT_HINTS[level]}`);
    });
    heat.addEventListener("change", () => persistInputs({ heat: Number(heat.value) }));
    query("[data-stage-build]").addEventListener("click", build);
    query("[data-brief-expand]")?.addEventListener("click", expand);
    query("[data-brief-undo]")?.addEventListener("click", undoExpand);
    query("[data-stage-send]").addEventListener("click", send);
    query("[data-stage-reset]").addEventListener("click", () => reset());
    query("[data-stage-compile]").addEventListener("click", () => host.compile());
    query("[data-stage-message]").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); send(); }
    });
    query("[data-generic-copy]").addEventListener("click", () => {
      const text = documentText();
      if (!text) { host.notify("Generic prompt", "Nothing has been built yet."); return; }
      host.copy(text, "Generic prompt copied");
    });
    query("[data-generic-toggle]").addEventListener("click", (event) => {
      const body = query("[data-generic-body]");
      const open = body.hidden;
      body.hidden = !open;
      event.currentTarget.textContent = open ? "Hide" : "Show";
      event.currentTarget.setAttribute("aria-expanded", String(open));
    });
    query("[data-generic-groups]").addEventListener("click", (event) => {
      const rename = event.target.closest("[data-rename-person]");
      if (rename) { startRename(rename); return; }
      const value = event.target.closest("[data-generic-value]");
      if (value) startEdit(value.closest(".h3ps-generic-row"));
    });
    query("[data-generic-groups]").addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      const value = event.target.closest("[data-generic-value]");
      if (!value) return;
      event.preventDefault();
      startEdit(value.closest(".h3ps-generic-row"));
    });
    query("[data-goal-list]").addEventListener("click", (event) => {
      const row = event.target.closest("[data-goal-id]");
      if (!row) return;
      if (event.target.closest("[data-goal-toggle]")) {
        goalAction({ action: "toggle", id: row.dataset.goalId, enabled: row.dataset.enabled !== "true" });
      } else if (event.target.closest("[data-goal-delete]")) {
        goalAction({ action: "delete", id: row.dataset.goalId });
      }
    });
    const goalText = query("[data-goal-text]");
    const addGoal = async () => {
      const text = goalText.value.trim();
      if (!text) return;
      // Cleared only once the ledger has actually taken it, so a refusal leaves
      // the wording in the box to edit rather than losing it.
      const before = session?.goals?.length ?? 0;
      await goalAction({ action: "add", text });
      if ((session?.goals?.length ?? 0) > before) goalText.value = "";
    };
    query("[data-goal-add]").addEventListener("click", addGoal);
    goalText.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); addGoal(); }
    });
    query("[data-delivery-target]").addEventListener("change", (event) => {
      const value = event.target.value;
      try {
        if (value.startsWith(AUTO_PREFIX)) {
          autoTargetId = value.slice(AUTO_PREFIX.length);
          applyAuto();
        } else {
          autoTargetId = null;
          host.selectMode(value);
        }
      } catch (error) {
        // The picker must still reflect the choice even if the host's own sync
        // work fails; otherwise the button keeps offering the previous model.
        host.notifyError(error);
      }
      render();
      persistInputs({ mode: host.currentMode() });
    });
    // Optional on the host: a studio that does not offer the setting still gets
    // a working bar, with the same default the backend would have used.
    const repairs = query("[data-repair-attempts]");
    const maxRepairs = host.maxRepairAttempts?.() ?? 5;
    repairs.innerHTML = Array.from({ length: maxRepairs + 1 }, (_value, count) =>
      `<option value="${count}">${count === 0 ? "None" : count}</option>`).join("");
    repairs.value = String(host.repairAttempts?.() ?? 3);
    repairs.addEventListener("change", (event) => host.setRepairAttempts?.(Number(event.target.value)));
    query("[data-delivery-variant-select]").addEventListener("change", (event) => {
      persistInputs({ mode: host.currentMode(), variant: event.target.value });
    });
    query("[data-negative-copy]").addEventListener("click", () => {
      host.copy(query("[data-negative-value]").value, "Negative prompt copied");
    });
  }

  // ---------------------------------------------------------------- setup

  return {
    async start() {
      mount();
      registry = await getTargets();
      applySession(await getSession(host.sessionId()));
      host.applyInputs?.(session?.inputs);
      if (session?.target?.mode) {
        host.selectMode(session.target.mode, { silent: true });
      } else {
        const inferring = targetsForStage().find((target) => target.infers_mode);
        if (inferring) {
          autoTargetId = inferring.id;
          applyAuto();
        }
      }
      render();
      return registry;
    },
    registry: () => registry,
    session: () => session,
    variantsFor: (mode) => targetFor(mode)?.variants || [],
    targetFor,
    targetsForStage,
    /** Called after a compile so the stored output, audit and goals refresh. */
    async afterCompile(result, mode) {
      applySession(await getSession(host.sessionId()));
      if (result?.prompt_audit) renderGoalSummary(result.prompt_audit);
      const negative = query("[data-negative-value]");
      negative.value = result?.negative_prompt || "";
      query("[data-negative-output]").hidden = !targetFor(mode)?.fields?.includes("negative_prompt");
      return session;
    },
    refresh: async () => applySession(await getSession(host.sessionId())),
    /** Clear scope: "prompts" keeps what you supplied, "all" is Reset. */
    clear: (options) => reset(options),
    /** Write the brief from one attached image (double-click on its card). */
    describe,
    busy: () => busy,
    /** Called when media is added or removed, so an automatic target follows it. */
    mediaChanged() {
      applyAuto();
      if (session) renderDelivery();
    },
    render,
    syncBusy,
    setDuration: (seconds) => persistInputs({ duration_seconds: seconds }),
    setAspectRatio: (ratio) => persistInputs({ aspect_ratio: ratio }),
    setBrief: (brief) => persistInputs({ brief }),
  };
}
