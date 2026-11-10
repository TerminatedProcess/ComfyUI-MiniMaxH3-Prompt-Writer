// A prompt handed over by another app (the Media Generator), and the GPU wait
// in front of every local-model call.
//
// The other app creates the session server-side and opens
// `/?session=<id>&autostart=1`. Autostart runs once per handoff -- a reload must
// not rebuild over what the user has since steered -- and goes through the same
// build and compile the user would click. Before either, the fleet gate waits
// for ComfyUI / InvokeAI to finish and switches the service preset; this module
// shows that wait and lets the user cancel it or go ahead anyway.

const PREFIX = "/h3studio";
const POLL_MS = 2000;
const RETRY_BASE_MS = 3000;
const ENGINE_ATTEMPTS = 5;
const COMPILE_ATTEMPTS = 2;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** LLM-Redirect answers 503 engine_not_set while the engine is still coming up:
 *  a documented, transient state that callers are meant to retry. */
export function isEngineNotReady(error) {
  return /engine_not_set|engine not set/i.test(`${error?.code || ""} ${error?.message || ""} ${JSON.stringify(error?.details || "")}`);
}

async function retrying(work, attempts = ENGINE_ATTEMPTS, delay = RETRY_BASE_MS) {
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await work();
    } catch (error) {
      if (!isEngineNotReady(error) || attempt + 1 >= attempts) throw error;
      await sleep(delay * 2 ** attempt);
    }
  }
}

async function call(fetchApi, path, body) {
  const response = await fetchApi(`${PREFIX}${path}`, body === undefined ? undefined : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload?.error?.message || payload?.message || `HTTP ${response.status}`);
    error.code = payload?.error?.code || payload?.code;
    throw error;
  }
  return payload;
}

/** Whether this page load asked to autostart a handoff; strips the flag so a reload will not. */
export function takeAutostart(location = globalThis.location, history = globalThis.history) {
  const params = new URLSearchParams(location?.search || "");
  if (params.get("autostart") !== "1") return false;
  params.delete("autostart");
  const query = params.toString();
  try { history?.replaceState(null, "", `${location.pathname}${query ? `?${query}` : ""}`); } catch {}
  return true;
}

/** The banner's words, from a gate snapshot. Empty string means hide it. */
export function bannerText(gate) {
  if (!gate?.enabled) return "";
  if (gate.phase === "waiting") return `Waiting for the GPU: ${gate.message || "another app is busy"}.`;
  if (gate.phase === "switching") return `Switching services to "${gate.preset}"…`;
  return "";
}

export function bannerMarkup() {
  return `<div class="h3ps-gate-banner" role="status" aria-live="polite" data-gate-banner hidden>
    <span data-gate-text></span>
    <button type="button" data-gate-proceed title="Starts the writer's services now, which stops InvokeAI / ComfyUI. Their running jobs are lost.">Go ahead (stops their jobs)</button>
    <button type="button" data-gate-cancel>Cancel</button>
  </div>`;
}

export function createHandoff({ root, fetchApi, isBusy, onError, retryDelayMs = RETRY_BASE_MS }) {
  const banner = root.querySelector("[data-gate-banner]");
  let acquiring = false;
  let timer = null;

  async function poll() {
    if (!acquiring && !isBusy()) {
      banner.hidden = true;
      return;
    }
    try {
      const gate = await call(fetchApi, "/fleet-gate");
      const text = bannerText(gate);
      banner.hidden = !text;
      banner.querySelector("[data-gate-text]").textContent = text;
      banner.querySelector("[data-gate-proceed]").hidden = gate.phase !== "waiting";
      banner.querySelector("[data-gate-cancel]").hidden = gate.phase !== "waiting";
    } catch {
      banner.hidden = true;
    }
  }

  banner.querySelector("[data-gate-proceed]").addEventListener("click", () => call(fetchApi, "/fleet-gate/proceed", {}).then(poll).catch(() => {}));
  banner.querySelector("[data-gate-cancel]").addEventListener("click", () => call(fetchApi, "/fleet-gate/cancel", {}).then(poll).catch(() => {}));
  timer = setInterval(poll, POLL_MS);

  return {
    poll,
    stop: () => clearInterval(timer),
    /** Run a handoff's first build + compile, once. */
    async autostart({ sessionId, session, refreshModels, connectLlm, selectMode, build, compile, hasModel }) {
      if (!session?.external) return false;
      const { first } = await call(fetchApi, `/external/session/${encodeURIComponent(sessionId)}/autostarted`, {});
      if (!first) return false;
      let gate = null;
      const fail = (title, message) => {
        onError(title, message);
        call(fetchApi, `/external/session/${encodeURIComponent(sessionId)}/error`, { message: `${title}: ${message}` }).catch(() => {});
        return false;
      };
      acquiring = true;
      poll();
      try {
        // Before the model list is read: under the image-server presets the
        // local LLM is not running, so there would be no model to select yet.
        gate = await call(fetchApi, "/fleet-gate/acquire", { session_id: sessionId });
      } catch (error) {
        return fail(error.code === "GPU_WAIT_CANCELLED" ? "Handoff paused" : "Could not prepare the GPU",
          error.code === "GPU_WAIT_CANCELLED" ? "Stopped waiting. Press Build when you are ready." : error.message);
      } finally {
        acquiring = false;
        poll();
      }
      await refreshModels();
      // The tab loaded before the preset started the LLM, so whatever it picked
      // then is a fallback -- connect the engine the preset just brought up.
      if (gate?.llm_url) {
        try {
          await retrying(() => connectLlm(gate.llm_url), ENGINE_ATTEMPTS, retryDelayMs);
        } catch (error) {
          return fail("Could not connect the local LLM", `${gate.llm_url}: ${error.message || error}`);
        }
      }
      // A text-only fallback model forces T2VA when a picture is attached;
      // put the target the caller asked for back.
      if (session.target?.mode) selectMode(session.target.mode);
      if (!hasModel()) return fail("No prompt model selected", "Choose a model, then press Build and Generate.");
      try {
        await retrying(build, ENGINE_ATTEMPTS, retryDelayMs);
      } catch (error) {
        return fail("Build failed", error.message || String(error));
      }
      // Compile reports its own errors as a toast and resolves either way, so
      // success is read from the server: a stored prompt for the target.
      for (let attempt = 0; attempt < COMPILE_ATTEMPTS; attempt += 1) {
        if (attempt) await sleep(retryDelayMs * 2 ** attempt);
        await compile();
        const status = await call(fetchApi, `/external/session/${encodeURIComponent(sessionId)}`).catch(() => null);
        if (status?.result?.prompt) return true;
      }
      return fail("Compile failed", "The writer did not produce a prompt; the error is shown in the writer tab.");
    },
  };
}
