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
