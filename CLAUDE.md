# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A ComfyUI **UI extension**, not a workflow node pack. `NODE_CLASS_MAPPINGS` is
deliberately empty; `__init__.py` only registers `ComfyUI/models/LLM/` as a
folder path and imports `backend/routes.py`, whose import side effect registers
aiohttp routes on ComfyUI's `PromptServer`. All UI lives in `web/` and is served
via `WEB_DIRECTORY`. The extension writes prompt text — it never runs a video
model or queues a generation.

The same `backend/` + `web/` code is reused verbatim by a second shippable
product, the Windows **Standalone** in `standalone/`.

## Commands

```bash
npm ci --ignore-scripts                                   # required once; frontend tests need happy-dom

# Extension: Python suite (includes the Node frontend suite via a wrapper — do not run it twice)
PYTHONPATH=tests/ci_stubs python -m unittest discover -s tests -p "test_*.py"

# Single Python test
PYTHONPATH=tests/ci_stubs python -m unittest tests.test_sequence -v
PYTHONPATH=tests/ci_stubs python -m unittest tests.test_sequence.SequenceTests.test_name

# Frontend suite directly (tests/frontend_regressions.mjs imports every other .mjs)
node --test tests/frontend_regressions.mjs
node --test tests/media_subjects.mjs          # one module

# Standalone
PYTHONPATH=standalone:standalone/h3_standalone/compat:. python -m unittest discover -s standalone/tests -p "test_*.py"
node --test standalone/tests/frontend.mjs
```

`tests/ci_stubs/folder_paths.py` stands in for ComfyUI's `folder_paths` so the
backend imports outside a ComfyUI install. There is no linter or formatter
configured.

Release builds are PowerShell-only (`scripts/build_extension.ps1`,
`scripts/build_standalone.ps1`); both refuse a dirty checkout without
`-AllowDirty` and package only `git ls-files` output.

## Architecture

### The generic prompt is the centre of gravity

The user builds **one model-agnostic scene document** on the left of the studio,
and every model prompt on the right is compiled from it. Switching from H3 to
Krea 2 must not lose a fact, so **no fact may live in the compiled prose** — it
lives in `backend/generic.py`.

- `generic.py` — the document. Named fields, each carrying an **origin**
  (`unspecified` / `invented` / `asset` / `user` / `override`) that records where
  the value came from, which is what lets a correction be applied to the right
  layer. Subject fields repeat **per person**: `wardrobe` is A's, `wardrobe#2`
  is B's, so an attribute is bound to a body rather than to a shared string.
- `conversation.py` — assembles the build / turn / expand / describe requests
  and folds the answers back in. Origins are assigned **structurally**, never by
  asking the model to classify its own output (measured at 0/6 on this stack).
- `goals.py` — standing instructions, re-injected into every generation and
  re-checked on every compile. A goal is not a patch.
- `scene_bible.py`, `person_names.py`, `pose_probe.py`, `heat.py` — supporting
  policy: distinctive-token checks, human handles, a second opinion on pose, and
  the Naughty dial.
- `session_store.py` — durable state on disk under `data/sessions/`, atomic
  writes. The document, goals, conversation, inputs and compiled outputs all
  survive a restart. Uploaded media deliberately does not.

### Targets

`backend/targets/` is the registry: one owner per mode, one place the UI reads.
`h3.py`, `krea2.py`, `anima.py` plus `base.py`. Adding a model is a spec module
and a vendored guide — **not** a new branch in the pipeline, the route handlers
or the studio markup. Six duplicate mode lists is how "Krea 2 works but its
Settings card is missing" happens.

### Request flow

`web/main.js` → `web/api/*.js` → routes under `/h3studio` → `assembly.py`
(validate + build messages from the vendored guide) → `h3_pipeline.py`
(shared media messages, Thinking fallback, prompt audit, fact insertion, narrow
repair, normalized metrics) → one provider backend's narrow `complete(...)` callable.

An audit failure is **additive or structural**, and they are repaired
differently. A dropped locked fact is absent text: `fact_insertion.py` writes
the missing sentences into the draft through the target's `integrate_facts`
hook and never re-emits the prompt, so the pass cannot lose anything. Everything
else is wrong text and goes to the narrow-repair loop, which does re-emit — and
therefore runs *after* insertion. The backstop is inside the pass, not after it:
when the model will not write an acceptable sentence, one is built from the
document value. There is no second pass, because `accept_repair` keeps a draft
with a dropped fact out of the candidate list. Do not move a dropped fact back
into the rewrite loop: a correction that returns all 450 words is how three
passes left five facts missing.

`backend/routes.py` owns a single module-level `STATE` dict guarded by
`STATE_LOCK`. Generation and media mutation are mutually exclusive — a busy
writer returns `GENERATION_BUSY` (409). Media lives in a per-session store
(`backend/media.py`, `STORE`) under ComfyUI's temp directory, wiped on import.

### Provider backends (`backend/models/`)

Four independent adapters, all registered in `routes.BACKENDS`: `gguf`
(llama-cpp-python, in-process), `external` (already-running `llama-server` over
HTTP/SSE), `ollama` (native Ollama contract), `api` (one generic Chat
Completions transport with OpenAI/Gemini/OpenRouter/Custom **presets**, not four
backends). They do **not** inherit from each other.

`models/contract.py` is the shared seam: `ModelError(code, message, details)`
and `final_message_text(...)`, which strips Gemma `<channel|>`/Qwen `<think>`
reasoning and raises `THINKING_TRUNCATED` rather than leaking reasoning into a
prompt. Never let reasoning text reach the final prompt.

API keys stay in backend memory only. They are never read from the environment
and never written to browser storage.

### GGUF model identification — three separate layers

Keep these distinct; conflating them is the recurring bug:

- `gguf_metadata.py` — reads the file's real metadata/architecture.
- `models/gguf_adapters.py` — *loading* compatibility only (which architectures
  and `mmproj` projectors llama-cpp-python can open, plus runtime version gates).
- `models/gguf_policies.py` — *sampling/reasoning* policy by resolved lineage.
  A custom `qwen35`/`qwen3vl` model does not inherit Qwen 3.8 policy from its
  architecture alone.

`models.json` lists downloadable verified model + projector pairs with VRAM
estimates; `catalog.py` merges it with what is actually on disk. A missing
projector must degrade to text-only, never block discovery.

### Reference media and subjects

Reference assets carry a `subject` (1..`generic.MAX_PEOPLE`, or `None`). The
studio shows an unassigned tray plus one box per person; a box is a person, and
pictures in it are the same body from different angles. **A picture with no
subject describes nobody** — it is scene, style or wardrobe evidence, and that
is also the no-op migration path for sessions that predate the feature.

Three rules that are easy to break and expensive to find:

1. **A reference tag is sticky.** `_assign_reference_identity` keeps an existing
   `<Picture N>` for the life of the file. Grouping, reordering boxes and
   editing must never renumber it — a tag already typed into the brief has to
   keep pointing at the same file.
2. **Groups are numbered in the prompt, never lettered.** `_named_people` puts
   letters in the same message meaning *document people* ("A is Liz"); a second
   letter scheme would have one prompt asserting two things about "A".
3. **A name belongs to a person number, which is a box's position.** Anything
   that shifts positions must carry the names with it, or Bob's pictures end up
   labelled Liz and the next build describes him by her name.

Box order is person order, so reordering is written in one pass
(`MediaStore.order_subjects`): applied as single moves, two boxes would briefly
share an index and merge into one person.

### Sequence

`backend/sequence*.py` turns one brief into several independent prompts.
`sequence.py` is documented as having no host/provider/graph dependencies —
preserve that. Two output formats: `official` (MiniMax structure, validated by
`sequence_output.py`) and `compact` (prose + sound/music fields, validated by
`sequence_format.py`), with `sequence_repair.py` for fail-closed recovery.

### Guides

`guides/*.md` are vendored **verbatim** from the official
`MiniMaxAI/MiniMax-H3` HF repo at a pinned revision. `backend/guides.py`
verifies their SHA-256 before use. Do not edit them or pass off community
prompting advice as official guide content.

### Frontend

Plain ES modules, no build step or bundler. `web/main.js` (~4.8k lines) is the
shell; **behaviour worth testing is extracted into small modules**
(`studio_state.js`, `media_subjects.js`, `media_visual.js`, `mode_inference.js`,
`writer_controls.js`, `compat.js`, …) that the Node suites import directly.
`main.js` imports `/scripts/app.js` from ComfyUI, so anything testable must live
outside it. Host differences are negotiated through `app.h3psHost` /
`HOST_CAPABILITIES` so the Standalone can disable ComfyUI-only features.

`web/writer_stage.js` owns the generic-prompt panel and is the only module that
talks to the `/generic` routes; `main.js` reaches it through `studio.stage`.

Rules the frontend keeps:

- **Component CSS uses theme tokens, never literal colours.** A maintenance
  guard in `tests/frontend_regressions.mjs` fails the build otherwise; add the
  colour to `web/styles/tokens.css`.
- **A constant duplicated in JS needs a parity test.** `mode_inference.js` and
  `media_subjects.js` both mirror Python values, and
  `tests/test_frontend_parity.py` parses the source and holds them to it. Assert
  the *values*, not just the length — `["1","2",…]` passed a length-only check.
- **No blocking dialog in front of feedback.** `window.confirm` freezes the
  renderer; show the progress indicator before the first `await`, or a slow
  operation is indistinguishable from a hang.

### Standalone (`standalone/`)

An aiohttp host that puts the upstream repo and `h3_standalone/compat/` (fake
`folder_paths` + `server`) on `sys.path`, imports the real `backend.routes`, and
serves `web/` from the upstream checkout — so **frontend edits are live on
reload, backend edits need a restart**. It then monkey-patches a **narrow seam**
in `app.py`: only Direct GGUF discovery, diagnostics, and execution are replaced
(with a managed external `llama-server` subprocess). Request assembly, pipeline,
and all other providers stay upstream. Keep that seam narrow — do not fork
pipeline logic into `standalone/`.

## Versioning

Two independently versioned products, enforced by
`standalone/tests/test_version_contract.py`:

- Extension: `pyproject.toml` `version` **must equal** `backend/version.py`
  `VERSION`; `CHANGELOG.md`.
- Standalone: `standalone/VERSION`, and it must **differ** from the extension
  version. Bumping it also requires updating the
  `H3-Prompt-Writer-Standalone-Windows-v<version>.zip` link in `README.md`,
  `standalone/README.md`, `standalone/RELEASE_NOTES.md`, and a matching top
  entry in `standalone/CHANGELOG.md`.

`.comfyignore` keeps `standalone/`, `scripts/`, and `tests/` out of the Comfy
Registry package; the contract test asserts those entries stay.

## Conventions

- Errors are structured `{code, message, details}` with stable uppercase codes
  (`GENERATION_BUSY`, `THINKING_TRUNCATED`, `INVALID_SEQUENCE`,
  `MEDIA_PREPARATION_FAILED`, `INVALID_SUBJECT`). The frontend keys off the code.
- **`<Picture N>` / `<Video N>` / `<Audio N>` are non-negotiable** in a compiled
  H3 prompt — the vendored guide requires them and `prompt_audit` rejects a
  prompt that drops one. The studio may *display* a shorter label (S1, V1, A1),
  but the canonical form is what is inserted into the brief and emitted.
- **A person's name never reaches the finished prompt.** It is a handle for
  talking to the writer; at compile time it is resolved back into the
  description, because a recognisable name drags the render toward whoever the
  model thinks it belongs to.
- Audio references are declared, never analyzed — no backend may infer unheard
  audio content; the role must come only from the user's brief.
- Video understanding is the ordered contact sheet shown in the preview, not the
  full encoded video.
- Comments explain **why**, especially the measurement or the bug behind a
  decision. The existing code does this consistently; match it.
- Commits follow Conventional Commits with a scope (`fix(gguf):`,
  `feat(sequence):`, `feat(media):`, `chore(release):`).
- `CHANGELOG.md` is user-facing prose, not a commit dump.

## Repository layout

This fork (`TerminatedProcess/ComfyUI-MiniMaxH3-Prompt-Writer`) is the only
working copy on this machine; work happens on the `mryan` branch. `upstream`
(`duckyshell/…`) is fetch-only and push is disabled. A second, stale clone used
to live under `~/comfy/prompt-gen/minimax-h3/` — it was deleted on 2026-10-10
because tooling kept reading its out-of-date copy of this file instead of the
repo being edited.
