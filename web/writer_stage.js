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
  editGenericField,
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
        <label class="h3ps-toggle-control" title="Adult or explicit content is permitted when your brief asks for it">
          <input type="checkbox" data-stage-flag="nsfw"><span></span>Naughty
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
        <button class="h3ps-text-button" type="button" data-negative-copy>Copy</button></header>
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
    groups.innerHTML = session.groups.map((group) => `
      <section class="h3ps-generic-group">
        <h4>${escape(group.title)}</h4>
        ${group.fields.map(fieldRow).join("")}
      </section>`).join("");
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
    // Say what the switches will do, before a generation proves it. They edit a
    // system prompt nobody reads, so without this they are invisible until the
    // output lands -- and by then it is too late to have chosen differently.
    const story = session.inputs?.story !== false;
    const nsfw = session.inputs?.nsfw !== false;
    query("[data-flag-effect]").textContent = [
      story
        ? "Gaps get invented detail you can replace."
        : "Only what you or your references supply; gaps stay unspecified.",
      nsfw ? "Adult content is allowed where you ask for it." : "Kept non-explicit.",
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
      previousBrief = payload.previous_brief ?? before;
      query("[data-brief-undo]").hidden = false;
      host.notify("Brief expanded", "Read it over and edit anything you disagree with, or undo it.");
    });
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
      applySession(await resetSession({ session_id: host.sessionId(), scope, clear_media: clearMedia }));
      if (scope === "all") host.afterReset();
      done = true;
      if (!notify) return;
      host.notify(
        scope === "prompts" ? "Prompts cleared" : "Session reset",
        scope === "prompts"
          ? "The generic prompt and every compiled prompt were cleared. Your media, brief, goals and conversation were kept."
          : "The generic prompt, goals, conversation and compiled prompts were cleared.",
      );
    });
    return done;
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
    query("[data-stage-build]").addEventListener("click", build);
    query("[data-brief-expand]")?.addEventListener("click", expand);
    query("[data-brief-undo]")?.addEventListener("click", undoExpand);
    query("[data-stage-send]").addEventListener("click", send);
    query("[data-stage-reset]").addEventListener("click", () => reset());
    query("[data-stage-compile]").addEventListener("click", () => host.compile());
    query("[data-stage-message]").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); send(); }
    });
    query("[data-generic-toggle]").addEventListener("click", (event) => {
      const body = query("[data-generic-body]");
      const open = body.hidden;
      body.hidden = !open;
      event.currentTarget.textContent = open ? "Hide" : "Show";
      event.currentTarget.setAttribute("aria-expanded", String(open));
    });
    query("[data-generic-groups]").addEventListener("click", (event) => {
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
