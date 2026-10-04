# Handoff

## Parked: guided story writer (design agreed, one decision open)

Discussed 2026-09-29/10-02. No code written yet. Resume from the open fork below.

### Goal

Replace the **Story builder** checkbox with a **guidable story writer**: click a button
and the AI writes a story from the reference image + the Naughty level, then you steer it
in conversation. Not a one-shot generator — a collaborator.

Driving example: an image of a woman lying on her bed, brief says *"the woman gets up and
walks to her kitchen, the camera follows her."* At Clean the writer invents breakfast with
her children; at Sensual she calls a plumber who turns out to be a handsome young man; at
Explicit/Graphic that goes where the slider says.

### Blocking finding: heat is a register dial, not a plot dial

The example is **impossible today at every level**. Every clause in `heat.py` forbids
invention:

- Sensual — *"Stay inside what the user wrote… never replace it with a different one"*
- Explicit — *"never swap it for a different act or a different person"*
- Graphic — *"keep their people, their setting and their act"*
- `HEAT_OVERRIDES_FAITHFUL` — *"invent nothing else — no new places, props, events or people"*

So the plumber is forbidden at Graphic. The dial controls **how explicitly to describe what
is there** (register); the feature needs **what happens next** (plot).

**Fix:** one slider, two clause families off the same level.

- *Register clauses* (existing, unchanged) → `build`, `turn`, compile.
- *Arc clauses* (new) → the story lane only. This becomes the single place in the app where
  inventing new people and events is licensed, so nothing already tuned regresses.

Arc clause intent per level: Clean = everyday continuation · Allowed = neutral unless the
brief points adult · Sensual = may introduce a person and a charged situation, no acts ·
Explicit = goes to sex, named plainly · Graphic = same arc, crude register.

Note the example proves heat must drive **both** what gets invented *and* how it is
described — which is why both families key off the one level.

### Core structural choice: beats, not prose

A story is an **ordered list of beats**, never a paragraph. Prose makes every instruction a
total rewrite and you lose what you liked; beats are addressable.

```
1. She's lying on her bed.                          [from image]
2. She gets up and walks to the kitchen, camera follows.  [user's words — locked]
3. She notices the tap dripping, calls a plumber.   [invented]
4. He arrives — young, easy smile.                  [invented]
```

Containment rule that keeps it safe: **the user's brief is the opening beats, preserved in
order; invention is licensed only forward in time.** Never rewrites backward, never
contradicts the image's starting state. Trivially checkable on a list.

This also collapses "Story" and "Extend brief" into **one contract at two starting points**:
Story writes forward from image + brief; Extend writes further forward from what exists.

### The lane

`turn` (`conversation.py:98`) is already this pattern — message in, `{reply, patch,
observed, goals}` out, steering a structured artifact. The story lane is the same shape with
`beats` in place of scene fields. Two conversations, clean split:

- **Story lane** — what happens (beats, narrative)
- **Document lane** — what the shot looks like (fields, pose, staging)

Reuses `record_turn`, `session_store`, the goals ledger, `_single_call`, the composer UI,
`clean_expanded_brief`, and the existing `previous_brief` undo plumbing.

### Guidance verbs → contract

| User says | Operation |
|---|---|
| "keep going" | append beats forward |
| "she has no children" | rewrite one beat |
| "she showers first" | insert a beat |
| "drop the phone call" | cut a beat |
| "he leaves without touching her" | re-aim the ending, earlier beats untouched |
| "go further from when he arrives" | re-run from beat *N* at higher heat |
| "keep beat 2 exactly" | lock it |

**Beat locking** is the mirror of `ORIGIN_USER` field protection and is what stops iteration
feeling like a gamble. The user's own typed beats are locked by default.

### Heat becomes per-instruction

Not a global verdict on the plot: the slider is the **default register for new beats**,
overridable per instruction (beats 1–4 Sensual, "go further from here" takes 5+ to Explicit).

### Decided along the way

- **`story` boolean survives as provenance, not a control** — hidden, set when the story lane
  wrote the brief. Still needed: `brief_supports` (`conversation.py:261`) depends on
  story-ness, and the krea2/anima default system prompts read it (`settings.js:134`).
- **Origin/locking bug to avoid.** An invented person landing in the brief box would be
  credited to the user by `brief_supports`/`quoted_from_brief` and **locked** — the comment
  at `conversation.py:131` calls that "the expensive mistake", and a rebuild could then never
  revise the plumber. Fix is nearly free: run the origin check against the **pre-story
  brief** (already retained for undo). User's typed words stay locked, inventions stay
  rewritable.
- **Undo must become a stack** (bounded ~10) in session inputs. One slot cannot serve a
  repeatable operation.
- **Sequence mode needs no decision.** Beats *are* chunks — `sequence.py` already wants
  `{brief, instructions, chunks[]}` with ids. A 5-beat story becomes a 5-chunk sequence; a
  2-beat story at 6s stays a single H3 prompt. Beat count picks the destination, so the user
  never picks a mode. (`shot_budget()` gives 1–2 shots at 6s, 3–5 at the 15s ceiling — the
  plumber story is 5+ beats and cannot be one H3 prompt.)
- **Standing goals work unchanged.** "Never mention her children" is a `presence`/`judged`
  goal in `goals.py`, injected into every build and compile and verified — story guidance
  becomes permanent with no new machinery.
- **`_probe_pose` will contradict on every story with motion** (image says lying, shot says
  walking). It corrupts nothing — it changes nothing and asks — but it should be quiet when
  the brief has story provenance, or you get a spurious question on every build.
- **Accepted loss:** the *faithful* expand path (`EXPAND_FAITHFUL_CLAUSE`, "tighten, invent
  nothing") disappears with the switch. Either keep it as a separate "Tighten" control or
  drop it — undecided, low stakes.

### OPEN FORK — answer this first

**Who owns the brief text once a story exists?**

1. **Beats own it** *(recommended)* — the brief box renders the beats read-only, with an
   "unlink" escape to freeform. No sync trap.
2. **Text owns it** — hand-edit prose freely, beats re-parse from it. Friendlier, but
   two-way prose↔structure sync is where this kind of feature rots.

Next step after that: run `plan-think` for a real implementation plan.

### Scope sketch

New stage + lane, mostly additive. Genuinely new: the beats model, the story turn contract,
one UI panel. Touches `backend/conversation.py`, `backend/generic_routes.py`, `backend/heat.py`
(arc clauses), `web/writer_stage.js` (button swap at ~:143 and the new panel), `web/main.js`
(api client), `tests/test_conversation.py`. Plus CHANGELOG + `backend/version.py` bump —
currently 0.4.7 (bumped for the repair-loop fixes).

### Note on resuming

At the time of this discussion the tree had uncommitted work on several of the files this
feature touches (`conversation.py`, `generic.py`, `generic_routes.py`, `goals.py`,
`session_store.py`, `targets/base.py`, and tests). Check that state before starting.
