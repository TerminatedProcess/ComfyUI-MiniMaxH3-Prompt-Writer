# Plan — Imagination, Conversation, and Asset Awareness

Fork-specific plan for the `mryan` branch. Not upstream work.

## Who this is for

A user who wants to make videos but is not a writer and has no time. They have
assets (photos, clips) and a feeling. They should be able to type
*"Bob is sitting in his living room"* and get a complete, cinematic, H3-valid
prompt — then steer it by talking, never by filling in forms.

They do not pre-specify. **They react.**

## The precedence ladder

Everything follows from this ordering:

```
1. ASSETS      images/video the user supplied     never contradicted
2. USER WORDS  brief + every conversation turn    overrides invention
3. INVENTION   everything not specified           filled freely, by default
```

Imagination is **on by default**. There is no toggle and no review gate — the
generated prompt *is* the review surface. An explicit instruction may override
an asset (tier 2 over tier 1); when that happens the UI says so. Silent drift
away from an asset is a bug, not an override.

---

## Calibration findings (measured, not assumed)

Ten briefs were run through a candidate expander against the local
`qwen3-vl-8b-abliterated` on :8890, ~4s each, 133–219 words. Raw output in
`.lab/expand/`. Expansion quality was good immediately: *"Bob is sitting in his
living room"* produced a mustard-yellow corduroy couch, scuffed floral
wallpaper, dusty afternoon light and a tungsten bulb, unprompted.

The **conversational edit** is where the design was decided. Three approaches
were tested with the instruction *"make the living room 70s style"*:

| Approach | Thoroughness | Continuity | Verdict |
|---|---|---|---|
| Edit the prose in place | ✗ relabelled the era, kept anachronistic VHS tapes even after a prompt rewrite demanding a dependency sweep | ✓ kept everything | reject |
| Re-expand from scratch | ✓ avocado shag carpet, leisure suit, walnut cabinet | ✗ **silently lost Bob** — subject became a different man | reject |
| **Scene bible + re-expand** | ✓ VHS → cathode-ray tube television | ✓ Bob, his age, his denim jacket, the push-in all survived | **adopt** |

The failure of edit-in-place is structural, not a wording problem: given a long
text and "change X", the model anchors on the existing prose and makes surface
substitutions. Two rounds of prompt engineering did not move it. Do not retry
this approach.

### Consequence: state is structured, never prose

Conversation state is a small **scene bible**, not the generated text:

```
subject   Bob — man, late 30s, unkempt brown hair, faded denim jacket
action    sitting on the couch, stares ahead, rubs neck, sighs
location  his living room
era       1970s
camera    slow push in, single continuous shot
```

Each turn makes a targeted edit to *bible fields* (reliable — they are short and
structured), then the prose is **regenerated** from the whole bible. This is why
it is both thorough and continuous: nothing anchors on stale prose, and nothing
drifts because every established fact is restated as a constraint.

The bible is also where asset locks attach, and its fields map onto the H3 node
inputs — so it is one structure serving three jobs.

---

## Pipeline

```
 user types / drops assets
        │
        ▼
 intent resolver ──> mode + duration + shot budget    (inferred, shown in plain English)
        │            + LoRA candidates matched from distilled concepts
        ▼
 SCENE BIBLE  (structured state; asset facts locked)
        │
        ▼
 EXPAND   (regenerates prose from bible; invents everything unspecified)
        │
        ▼
 ASSEMBLE (existing, literal — expanded brief is now the factual boundary)
        │
        ▼
 AUDIT ──> format rules + LoRA trigger survival + asset locks
        │      │
        │      └── violation ──> narrow repair turn ──┐
        ▼                                             │
   prompt shown ◄──────────────────────────────────────┘
        │
 "make it 70s" ──> update bible field ──> EXPAND ──> ... (loop)
```

No cycles. The repair loop is bounded by the existing retry cap.

Stage ASSEMBLE is **unchanged**. Its system prompt currently forbids invention
(*"treat the user's brief and supplied references as the factual boundary"*) and
that rule stays exactly as-is — it now protects format fidelity rather than
suppressing creativity, because the brief it receives is already rich.

---

## LoRA awareness

The user has ~122 MiniMax-H3 LoRAs in HubRoot. These act on the **video** model,
not the LLM, so the prompt generator cannot run them — it must write prompts
that *activate* them.

Measured coverage (join `api_cache.cache_key = models.hash_sha256` — **not**
`hash_blake3`, which returns zero rows):

| | |
|---|---|
| H3 LoRAs | 122 |
| with a cached Civitai record | 122 (100%) |
| with `trainedWords` | 54 |
| with a description | 48 |
| **with any usable signal** | **83 (68%)** |
| with `preview_url` | 120 (98%) |

Most H3 video LoRAs have **no activation token at all** — they are always-on
motion/style adapters. A trigger-only feature would be dead weight for ~40% of
the library, so the distilled *concept* matters as much as the trigger.

- **Enrichment is offline and one-time**, reading the local cache. No network at
  generation time. Keyed by `hash_sha256`; file content is immutable, so each
  LoRA is distilled exactly once, ever.
- Output per LoRA: `{triggers[], concept|null, vocabulary[], mode_fit}`.
  **`concept: null` is valid** — Civitai descriptions are full of Discord
  invites and changelogs, and an invented concept is worse than none.
- Stored in a **sidecar SQLite owned by this app**, never in HubRoot's schema —
  another session owns that DB, and this keeps rollback to deleting one file.
- Picker is a **card grid** (`preview_url` + name + concept + auto-matched
  badge), with a link out to the existing Hub Model Card Browser on :8001 for
  full browsing. Thumbnails load lazily and degrade to a placeholder offline.
- **Triggers are hard-audited** (they gate activation, so a missing trigger
  earns a repair turn). **Vocabulary is soft** steering only.

`civitai-red-api-key` exists in the SOPS vault for an optional top-up of the 39
no-signal LoRAs, re-fetched at *model* level (the cache is version-scoped and
misses data that exists upstream). Read at startup via `sops -d --extract`;
**no key entry UI**, status display only, masked `first5…last5`.

### Caching

The DB read is single-digit milliseconds — not worth caching. The **distillation**
is the expensive part and is cached permanently. Freshness is a cheap validity
token, `(MAX(id), COUNT(*), MAX(updated_at))`, which detects the adds that
dominate this library and picks up soft-deletes via the `deleted` column. Only
new hashes get distilled.

---

## ComfyUI workflow topology (Phase 2)

Templates ship **on disk** in `comfyui_workflow_templates_json` (580 files, 15
MiniMax H3) — not the cloud, and not in the empty `comfyui_workflow_templates`
meta-package. Copies of the four relevant graphs are in `.lab/templates/`.

Learned grammar, using **core** nodes only:

- `MiniMaxH3ImageToVideo` serves **both** T2V and I2V — T2V is the same graph
  with `first_frame`/`last_frame` left unconnected. Inputs `clip`, `vae`,
  `prompt` (STRING), `width`/`height`/`length`; outputs `positive`
  (CONDITIONING) **and** `LATENT` directly.
- `MiniMaxH3ReferenceToVideo` is the root for Reference/Multiframe, with
  indexed `ref_images.ref_image_N` / `ref_videos` / `ref_audios` slots.
- Multiframe chains **three `MiniMaxH3AddGuide`** nodes in series, each adding
  one image at a `frame_idx` and passing `positive` forward.
- Tail is identical across all four: `RandomNoise` + `KSamplerSelect` +
  `BasicScheduler` → `BasicGuider`/`SamplerCustomAdvanced` → `VAEDecode` +
  `VAEDecodeAudio` → `CreateVideo` → `SaveVideo`.

`EmptyMiniMaxH3LatentAV` is unused in these graphs. `MiniMaxH3UnifiedToVideo`
and `MiniMaxH3ResolutionSelector` are **third-party** and not installed here —
do not build against them.

Because one node covers T2V/I2V and another covers Ref/Multiframe, Phase 2 is a
small graph builder, not a template-management system.

### VRAM

ComfyUI (~14.5 GB) and llama.cpp (~15.5 GB with the LoRA) cannot coexist on the
16 GB card. Image generation therefore requires a **swap**, automated using the
same logic as the `minirun`/`ministop` fish functions plus the existing
`web/vram_handoff.js`. Generate **4 candidates per swap** so the ~45–60s cost is
amortised rather than paid per image.

---

## Scope

Phase 1 is roughly 6 new backend/frontend modules and 6 edited files, plus
tests. **This exceeds 8 files** — stated explicitly. No new service, runtime, or
language: Python plus vanilla ES modules, matching the repo.

**Not building:** HubRoot schema changes, LoRA weight tuning, automatic LoRA
selection without a veto, non-H3 LoRAs, hosted-API (`Max`) workflow variants.

## Rollback

Every stage is additive behind one feature flag. Flag off = today's literal
behaviour, byte-identical. The LoRA sidecar is one deletable file. No HubRoot
schema change, no migration to reverse.

## Test paths

- Bare brief → complete prompt with invented detail
- Drop 1 image → I2VA inferred, alignment line correct
- Drop 3 images → Reference inferred, subjects defined
- `"make it 70s"` → era-dependent details all change; **no anachronisms survive**
- `"make it 70s"` with a subject photo → asset lock holds, subject unchanged
- `"put Bob in a 70s suit"` → lock overridden, note emitted
- `"make it longer"` → shot budget re-planned
- LoRA auto-match → trigger present in output, audit passes
- Model drops a trigger → repair turn restores it
- Sidecar absent → picker empty, generation unaffected
- Feature flag off → output identical to current behaviour

## Settled by testing (do not revisit)

**Sampling is not the lever.** The prose-editing failure was tested against the
hypothesis that a stale seed was locking the model in. It is not: the anachronism
survived **5/5 runs with five explicit different seeds**, while the outputs
themselves varied between seeds. It also survived two different system prompts.
The cause is conditioning — with the prior brief in context, the highest-
probability continuation is to copy it with light edits. Context shape drives
behaviour; sampling parameters drive phrasing only.

**There is no reproducibility guarantee.** Same prompt + same pinned seed, run
twice, produced *different* output on this stack (continuous batching across 4
slots, non-deterministic CUDA reductions, prompt-cache reuse — not isolated
further). Consequence: never offer "regenerate this exact prompt" backed by a
stored seed. **Store the generated text**, which is cheap and actually works.

**Names are all-or-nothing in lock checks.** Integration testing caught the
expander rendering a locked subject as "an adult male in his late 30s" — every
visual fact intact, the name gone — which passed a token-ratio check because the
name was one token in nine. A name is the handle the user steers with ("put Bob
in a suit"), so `lock_violations` now requires every proper noun in a locked
field to appear, independent of the ratio. This failure is intermittent, which
is exactly why it needs the audit rather than prompt wording.

## Built so far

- `backend/scene_bible.py` — structured conversation state, pure (no host,
  provider or graph dependencies), with `apply_update` enforcing asset locks and
  reporting explicit overrides, and `lock_violations` for the audit.
- `tests/test_scene_bible.py` — 26 tests. Full suite: **410 passing**, no
  regressions against the 384 that existed before.
- `scripts/enrich_loras.py` — LoRA distillation, idempotent, 122/122 in 2m18s.
- `.lab/` (git-excluded) — calibration outputs, the four H3 templates, the
  end-to-end integration harness (11/11, stable over three consecutive runs).

## Open items

- **Scope resolution** for edits naming a region ("the living room") is
  unmeasured. Mitigation: bible fields are coarse enough that most instructions
  map to a whole field; fallback is regenerating the whole brief with locks
  held, which is correct but blunter.
- Expander occasionally emits non-visual interpretation ("quiet stagnation").
  Tighten the "camera could see it" rule; low severity since Stage B discards
  unrenderable material.
