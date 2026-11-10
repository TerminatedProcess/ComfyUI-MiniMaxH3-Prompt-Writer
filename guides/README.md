# Vendored prompting guides

Every file here is copied verbatim from its vendor at one pinned revision.
`backend/guides.py` records the source URL and the SHA-256 of the normalized
content, normalizes only trailing line endings, and verifies each file before
use — a drifted guide fails loudly rather than silently changing what the writer
is told.

| File | Source | Revision | Used by |
|---|---|---|---|
| `VIDEO_PROMPT_WRITING_GUIDE_base_en.md` | `MiniMaxAI/MiniMax-H3` (Hugging Face) `docs/` | `bfc8ed0` | T2VA, I2VA, FL2VA, L2VA |
| `VIDEO_PROMPT_WRITING_GUIDE_ref_en.md` | same | `bfc8ed0` | Reference |
| `KREA2_PROMPTING_GUIDE_en.md` | `krea-ai/krea-2` (GitHub) `docs/prompting.md`, Apache-2.0 | `db3984f` | Krea 2 |
| `KREA2_PROMPT_EXPANSION_en.txt` | `krea-ai/krea-2` `docs/expansion.txt`, Apache-2.0 | `db3984f` | Krea 2 |
| `ANIMA_MODEL_CARD_en.md` | `circlestone-labs/Anima` (Hugging Face) `README.md` | `f973fc4` | Anima |
| `ILLUSTRIOUS_XL_MODEL_CARD_en.md` | `OnomaAIResearch/Illustrious-xl-early-release-v0` (Hugging Face) `README.md` | `dca0dac` | SD tags |
| `QWEN_IMAGE_2512_PROMPT_UTILS.py.txt` | `QwenLM/Qwen-Image` (GitHub) `src/examples/tools/prompt_utils_2512.py`, Apache-2.0 — the English `SYSTEM_PROMPT` is excerpted. Not the `Qwen-Image-2.1-PE-T2I` prompt: that is `qwen-research` (non-commercial) licensed | `6b5e1f5` | Qwen-Image |
| `FLUX3_KLEIN_PUP_PROMPTS_EXTERNAL_VLM.json` | `black-forest-labs/flux-3-image-klein` (GitHub) `src/flux3/pup_prompts_external_vlm.json`, Apache-2.0 — the `t2i` prompt is excerpted | `bfc8889` | FLUX 3 |
| `MOSS_IMAGE_USERGUIDE_en.md` | `OpenMOSS/MOSS-Image` (GitHub) `USERGUIDE.md`, Apache-2.0 — Prompt Guidelines only | `6be4453` | MOSS-Image |
| `MOSS_IMAGE_PROMPT_ENHANCER_SKILL_en.md` | same, `skills/moss-image-prompt-enhancer/SKILL.md` — Caption section only | `6be4453` | MOSS-Image |
| `PONY_V6_XL_DESCRIPTION_en.html` | CivitAI model 257749 `description` (API), version 290640; fetched 2026-11-10 — CivitAI has no revisions, so the hash is the pin | — | SD tags |

Two of them are excerpted before being sent to the model, because the whole file
would spend context on material that cannot help write a prompt: Krea's sample
images are stripped and its worked prompts kept; Anima's card is reduced to its
Prompting and Limitations sections, dropping installation, sampler settings,
finetuning and licensing. Both excerpts fail loudly if the expected sections
stop being present.

Our own system prompts live in `backend/targets/`, never here. That separation
is what lets the Naughty flag drop Krea's "assume clothing covers genitals"
clause from the instructions we send without editing a single byte of Krea's
pinned document.

Do not silently treat community prompting recipes as official guide content.
