# Multi-target Writer: one generic prompt, many models

Fork-specific, `mryan` branch. Not upstream work. Companion to
[PLAN_IMAGINATION_AND_ASSETS.md](PLAN_IMAGINATION_AND_ASSETS.md), whose
measurements this design is built on.

## What changed

The writer used to write one prompt for one model. Now it does two separate
things:

1. **Build a generic prompt** from your brief, your reference media and a
   conversation — a model-agnostic scene document.
2. **Compile it for a target** — MiniMax H3, Krea 2 or Anima today, from the
   same document, as many times as you like.

That split is the whole point. Facts live in the document, not in the compiled
prose, so switching from H3 to Krea 2 cannot lose one.

```
LEFT — build once                          RIGHT — deliver many times
media · duration · aspect                  GENERIC PROMPT
Naughty · Story builder          ───────▶  subject · wardrobe · action · …
brief  → [Generate]                        each field tagged with its origin
conversation  ◀──── patches ────▶          goals, each with a verdict
                                           ─────────────────────────────
                                           [model ▾] [variant ▾] [Generate]
                                           compiled prompt (+ negative)
```

## The generic prompt document

`backend/generic.py`. Nineteen ordered fields grouped as Subject / Scene / Look /
Sound / Constraints. Structured rather than prose because prose editing was
measured to fail: told to restyle a room to the 1970s the model renamed the era
and kept the VHS tapes, across five seeds.

Every field records where its value came from, and that origin decides who may
change it:

| Origin | Means | Locked into every compile | A re-observation |
|---|---|---|---|
| `unspecified` | nothing has fixed it | no | fills it |
| `invented` | Story builder filled it | no | may replace it |
| `asset` | read off your reference media | yes | re-derives it |
| `user` | you said it | yes | leaves it alone |
| `override` | you said it **and** it contradicts the image | yes | **never reverts it** |

`override` exists because these three corrections are otherwise
indistinguishable, and conflating them means a later re-read of the picture
silently undoes your choice:

- *the image shows red, the prompt never said a colour* → a gap, filled by
  re-observing (`asset`)
- *the image shows blue, you want red anyway* → a deliberate deviation
  (`override`, which records that the image showed blue)
- *the image shows red, the document wrongly says blue* → corrected by
  re-observing, or by your own words (`user`)

**Crediting your words.** A field you asked for, then expanded by Story builder,
is still yours. The model may name the fragment of your brief a field came from,
but that claim is verified against the brief rather than trusted; failing that,
a field counts as yours when it carries one of your own phrases (allowing for
inserted adjectives, so "blue skirt" survives becoming "cerulean blue silk
skirt"). Getting this wrong is expensive in one direction only: a fact of yours
filed as `invented` is never locked, and can drift away on the next compile.

## Goals

`backend/goals.py`. An instruction is not a patch. "Make sure the prompt follows
the clothing colours in the image" has to keep holding after the next
regeneration and after switching models, so goals are session state, injected
into every generation and every compile, and verified against each result.

Three verification kinds, because pretending one mechanism covers arbitrary
English would be dishonest:

| Kind | Example | Check |
|---|---|---|
| `field` | "follow the clothing colours in the image" | the named fields must be `asset` or `override` — never `unspecified` or `invented` |
| `presence` | "name the bakery sign" | the text must appear in the compiled prompt |
| `judged` | "don't make it feel like a commercial" | one verifier pass returning PASS/FAIL and a reason |

An unmet goal feeds the existing narrow-repair pass. If repair does not fix it,
the studio says so. A goal is satisfied by an `override` that contradicts it —
your choice outranks the picture, and the ledger says "met · your override".
A judged goal whose verdict never arrives stays *pending*; it is never counted
as met.

## Flags

Both default **on**, both are request fields, and both edit the system prompt
rather than replacing it. With both off, every wrapper is byte-identical to the
one this writer shipped with (`tests/test_targets.py` holds them to it).

- **Naughty** — adult content is permitted where the brief asks for it. Krea 2's
  own "assume clothing covers genitals" clause is dropped (their guide stays
  pinned and unedited; our wrapper is a separate layer). Anima tags safety from
  the brief instead of forcing `safe`.
- **Story builder** — the writer invents supporting detail the brief leaves open
  instead of staying literal. What stops it drifting is not a new mechanism: the
  document's locks and your explicit constraints still apply, and invention only
  fills gaps.

A saved system-prompt override in Settings still wins wholesale; the flags then
stop affecting that profile, which is stated on the card.

## Targets

`backend/targets/`. One spec per model: its modes, the guides it is written
from, its system prompt, its output shape, its audit and its repair contract.
Adding a model is a spec module plus a vendored guide — not another branch
through the pipeline, the routes or the studio.

| Target | Output | Guide (vendored, hash-pinned) |
|---|---|---|
| MiniMax H3 | official sections | MiniMax H3 base + reference guides |
| Krea 2 | one prose paragraph | `krea-ai/krea-2` `docs/prompting.md` + `docs/expansion.txt` (Apache-2.0, `db3984f`) |
| Anima | booru tag pair (positive + negative) | `circlestone-labs/Anima` model card (`f973fc4`) |
| MiniMax Music 3 | structured caption | own contract |

**Anima's variant is load-bearing, not cosmetic.** Per its model card a
`score_*` tag is correct on Base, harmful on Aesthetic ("pushes it too hard into
slop territory") and inert on Turbo, which runs at CFG 1 where the negative
prompt does nothing. Nothing in the output looks wrong when you get this wrong,
so the audit checks it and the repair pass restates the rule.

### Adding a target

1. Vendor its guide into `guides/`, pin it in `backend/guides.py` with the
   normalized sha256 and its upstream revision.
2. Write `backend/targets/<name>.py`: a `Target` with its modes, a system prompt
   composed from flag-driven clauses, an `audit`, a `repair_plan` and an
   `accept_repair`.
3. Register it in `backend/targets/__init__.py`.

Everything else — routes, media limits, context budget, Settings cards, the
studio's model picker — reads the registry.

## Session state

`backend/session_store.py`. The document, the goals, the conversation, the
left-rail inputs and the last compiled prompt per target live in
`data/sessions/<id>.json` and persist **until Reset**, not until a reload.
Writes are atomic.

One deliberate gap: uploaded media still lives in the media store's temp cache,
which is cleared when the server restarts. The document survives and records
what was attached, so the studio can say "these images are no longer loaded —
re-add them to re-read the picture" instead of failing a re-observation
silently.

## Living with a small local model

Measured against a local abliterated qwen3-vl-8b, which is the target
environment rather than an edge case:

- The document call runs at **lower sampling temperature** than prompt writing.
  A JSON contract is not creative work, and at the writer's normal temperature a
  19-field document intermittently stopped mid-string.
- A **truncated answer is salvaged**: complete fields before the cut are kept.
- A salvage that recovers **fewer than five fields is treated as a failure**, so
  the build retries rather than handing back a near-empty card.
- A parse failure reports **what the model actually said**, because "did not
  return a usable document" is the same message whether it refused, narrated or
  ran out of room.
- A standing instruction ("always…", "from now on…") that the model fails to
  record as a goal is kept **in your own words** rather than expiring with the
  turn.

## Tests

- `tests/test_targets.py` — registry invariants and the flag matrix, including
  the byte-identical flags-off guarantee.
- `tests/test_generic_prompt.py` — origins and the three correction cases.
- `tests/test_goals.py` — the ledger and its three verification kinds.
- `tests/test_conversation.py` — origin assignment, salvage, standing goals.
- `tests/test_image_targets.py` — Krea 2 and Anima audits, variants, safety tags.
- `tests/test_pipeline_targets.py` — end to end through the real pipeline.
- `tests/test_generic_routes.py`, `tests/test_session_store.py` — endpoints and
  persistence.
- `tests/writer_stage.mjs` — the studio, mounted for real in happy-dom.
- `tests/test_frontend_parity.py` — the one rule duplicated in Python and
  JavaScript (H3 mode inference) is held to a single table.
