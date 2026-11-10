from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Callable
from functools import lru_cache
from pathlib import Path


GUIDES_DIR = Path(__file__).resolve().parent.parent / "guides"
SOURCE_REVISION = "bfc8ed0353f5a9733be73e6b2c98ec0948195b86"
SOURCE_ROOT = f"https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/{SOURCE_REVISION}/docs"
KREA2_REVISION = "db3984fbc6e13b34c0064990fc2d95ac64d00058"
KREA2_SOURCE_ROOT = f"https://github.com/krea-ai/krea-2/blob/{KREA2_REVISION}/docs"
ANIMA_REVISION = "f973fc41ec7545364ac9776c2440285f43ff2a30"
ANIMA_SOURCE_ROOT = f"https://huggingface.co/circlestone-labs/Anima/blob/{ANIMA_REVISION}"
ILLUSTRIOUS_REVISION = "dca0dac303e6dc4b0c31d8001bc685b89b5d0204"
ILLUSTRIOUS_SOURCE_ROOT = (
    f"https://huggingface.co/OnomaAIResearch/Illustrious-xl-early-release-v0/blob/{ILLUSTRIOUS_REVISION}"
)
# Pony Diffusion V6 XL publishes its prompting rules only in the CivitAI model
# description; its Hugging Face repo README is empty. CivitAI has no content
# revisions, so the pin is the model-version id plus the SHA-256 below, and the
# file is the `description` field of /api/v1/models/257749 as fetched 2026-11-10.
PONY_REVISION = "civitai-model-257749-version-290640"
PONY_SOURCE_ROOT = "https://civitai.com/api/v1/models"
# Qwen-Image's prompt-enhancement system prompt, from the Apache-2.0 GitHub repo.
# Deliberately NOT the Qwen-Image-2.1-PE-T2I `system_prompt.txt`: that file is
# under the non-commercial `qwen-research` licence, and this repository is MIT.
QWEN_IMAGE_REVISION = "6b5e1f5cec987d404be5ac6657db3b9aacb56a89"
QWEN_IMAGE_SOURCE_ROOT = f"https://github.com/QwenLM/Qwen-Image/blob/{QWEN_IMAGE_REVISION}/src/examples/tools"
FLUX3_REVISION = "bfc8889a4497f846416f253a3972a1941ac698b9"
FLUX3_SOURCE_ROOT = f"https://github.com/black-forest-labs/flux-3-image-klein/blob/{FLUX3_REVISION}/src/flux3"
MOSS_REVISION = "6be4453024c5acae9dcd0a191e26ee524e743310"
MOSS_SOURCE_ROOT = f"https://github.com/OpenMOSS/MOSS-Image/blob/{MOSS_REVISION}"


@dataclass(frozen=True)
class GuideSpec:
    id: str
    title: str
    filename: str
    source_sha256: str
    # Every guide is vendored verbatim from one upstream revision. Krea and
    # CircleStone publish theirs in their own repositories, so the root and the
    # remote basename are per-guide rather than global.
    source_root: str = SOURCE_ROOT
    source_revision: str = SOURCE_REVISION
    source_basename: str | None = None
    vendor: str = "MiniMax"
    # Name of the function in this module that cuts the file down to what helps
    # write a prompt. None sends the whole file. Named rather than a callable so
    # the spec stays plain data (it is serialised into the guide catalogue).
    excerpt: str | None = None

    @property
    def source_url(self) -> str:
        return f"{self.source_root}/{self.source_basename or self.filename}"


GUIDES = {
    "base": GuideSpec(
        id="base",
        title="MiniMax H3 Video Prompt Writing Guide",
        filename="VIDEO_PROMPT_WRITING_GUIDE_base_en.md",
        source_sha256="2cfebc096a6e08370f288d468d90b60f7f9bcb938f94bf090816e910e48e75fc",
    ),
    "reference": GuideSpec(
        id="reference",
        title="MiniMax H3 Reference Prompt Writing Guide",
        filename="VIDEO_PROMPT_WRITING_GUIDE_ref_en.md",
        source_sha256="1e574f356716ad55612247ffb7bbccbcdb484ad96599d63c7dca1af186b1fab7",
    ),
    "krea2": GuideSpec(
        id="krea2",
        title="Krea 2 Prompting Guidelines",
        filename="KREA2_PROMPTING_GUIDE_en.md",
        source_sha256="9f05bdd218769c820b50fcd41ca1169121887bff7958a4a756a9c50c0a4e25cf",
        source_root=KREA2_SOURCE_ROOT,
        source_revision=KREA2_REVISION,
        source_basename="prompting.md",
        vendor="Krea",
    ),
    "krea2_expansion": GuideSpec(
        id="krea2_expansion",
        title="Krea 2 Official Prompt Expansion Instructions",
        filename="KREA2_PROMPT_EXPANSION_en.txt",
        source_sha256="2a5c24a3a83e9d415d4679a4e1853ddf056bd0329f3af315b27ed72b6ad9c2f0",
        source_root=KREA2_SOURCE_ROOT,
        source_revision=KREA2_REVISION,
        source_basename="expansion.txt",
        vendor="Krea",
    ),
    "anima": GuideSpec(
        id="anima",
        title="Anima Model Card",
        filename="ANIMA_MODEL_CARD_en.md",
        source_sha256="87ccc2ef185dabbc3cb8afd61996961446b6d5776dbd3de9f3cbfaa1e67c052e",
        source_root=ANIMA_SOURCE_ROOT,
        source_revision=ANIMA_REVISION,
        source_basename="README.md",
        vendor="CircleStone Labs",
    ),
    "illustrious": GuideSpec(
        id="illustrious",
        title="Illustrious XL Model Card",
        filename="ILLUSTRIOUS_XL_MODEL_CARD_en.md",
        source_sha256="0b4a69d83c4a3138cbb15bc6a29fc52609e94f06a1269fa66d2e16c5f1c3aaef",
        source_root=ILLUSTRIOUS_SOURCE_ROOT,
        source_revision=ILLUSTRIOUS_REVISION,
        source_basename="README.md",
        vendor="OnomaAI Research",
        excerpt="illustrious_prompting_excerpt",
    ),
    "pony": GuideSpec(
        id="pony",
        title="Pony Diffusion V6 XL Model Description",
        filename="PONY_V6_XL_DESCRIPTION_en.html",
        source_sha256="1624df08093f5e93dbdf5ea42ddfeca1696ce8aabbdd16f0bc0bd145b7d1b7b0",
        source_root=PONY_SOURCE_ROOT,
        source_revision=PONY_REVISION,
        source_basename="257749",
        vendor="PurpleSmartAI",
        excerpt="pony_prompting_excerpt",
    ),
    "qwen_image": GuideSpec(
        id="qwen_image",
        title="Qwen-Image Prompt Enhancement System Prompt",
        filename="QWEN_IMAGE_2512_PROMPT_UTILS.py.txt",
        source_sha256="87c0ad84d417ee116d73a51e85c880b9dd8caa7f206d567c557c663fa48360ba",
        source_root=QWEN_IMAGE_SOURCE_ROOT,
        source_revision=QWEN_IMAGE_REVISION,
        source_basename="prompt_utils_2512.py",
        vendor="Qwen",
        excerpt="qwen_image_prompting_excerpt",
    ),
    "flux3": GuideSpec(
        id="flux3",
        title="FLUX 3 Image Klein Prompt Upsampler (external VLM)",
        filename="FLUX3_KLEIN_PUP_PROMPTS_EXTERNAL_VLM.json",
        source_sha256="361bb6e081d50e54a3dee9abf26327436a9386c83465f881c7848989232028be",
        source_root=FLUX3_SOURCE_ROOT,
        source_revision=FLUX3_REVISION,
        source_basename="pup_prompts_external_vlm.json",
        vendor="Black Forest Labs",
        excerpt="flux3_prompting_excerpt",
    ),
    "moss_image": GuideSpec(
        id="moss_image",
        title="MOSS-Image User Guide",
        filename="MOSS_IMAGE_USERGUIDE_en.md",
        source_sha256="a03845c2cb582ea448e407ac7c81e7437daec1642509529461471fd82eda4976",
        source_root=MOSS_SOURCE_ROOT,
        source_revision=MOSS_REVISION,
        source_basename="USERGUIDE.md",
        vendor="OpenMOSS",
        excerpt="moss_image_prompting_excerpt",
    ),
    "moss_image_enhancer": GuideSpec(
        id="moss_image_enhancer",
        title="MOSS-Image Prompt Enhancer Skill",
        filename="MOSS_IMAGE_PROMPT_ENHANCER_SKILL_en.md",
        source_sha256="72cd39883af0b6c6997038b068af2d858dcd968ca43466ebf93fea21833878a0",
        source_root=MOSS_SOURCE_ROOT,
        source_revision=MOSS_REVISION,
        source_basename="skills/moss-image-prompt-enhancer/SKILL.md",
        vendor="OpenMOSS",
        excerpt="moss_image_caption_excerpt",
    ),
}
MODE_GUIDES = {
    "T2VA": "base",
    "I2VA": "base",
    "FL2VA": "base",
    "L2VA": "base",
    "Reference": "reference",
    "Krea2": "krea2",
    "Anima": "anima",
}


def _normalized(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"


@lru_cache(maxsize=len(GUIDES))
def load_guide(guide_id: str) -> dict[str, str]:
    spec = GUIDES[guide_id]
    content = _normalized((GUIDES_DIR / spec.filename).read_text(encoding="utf-8-sig"))
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if digest != spec.source_sha256:
        raise RuntimeError(f"Official guide integrity check failed for {spec.filename}.")
    return {
        **asdict(spec),
        "source_url": spec.source_url,
        "content_sha256": digest,
        "content": content,
    }


def guide_for_mode(mode: str) -> dict[str, str]:
    return load_guide(MODE_GUIDES[mode])


@lru_cache(maxsize=1)
def reference_base_excerpt() -> str:
    """Return only the shared base-guide rules referenced by full-reference mode."""
    content = load_guide("base")["content"]
    sections = content.split("\n### ")
    selected: list[str] = []
    paragraph_limits = {
        "4.2 Shots and Cuts": 1,
        "4.3 Camera Motion: Motion Type + Amplitude + Speed": 1,
        "4.4 Speakers, Dialogue, and Singing": 2,
        "4.5 On-Screen Text": 1,
        "4.6 overall_soundscape": 1,
        "4.7 non_diegetic_music": 1,
    }
    for section in sections:
        title, _, body = section.partition("\n")
        limit = paragraph_limits.get(title.strip())
        if limit is None:
            continue
        paragraphs = [part.strip() for part in body.split("\n\n") if part.strip()]
        prose = [part for part in paragraphs if not part.startswith(("```", "|", "## "))]
        selected.append(f"### {title.strip()}\n\n" + "\n\n".join(prose[:limit]))
    if len(selected) != len(paragraph_limits):
        raise RuntimeError("Could not extract the required shared rules from the official base guide.")
    return (
        "# Shared official base-guide rules used by full-reference mode\n\n"
        + "\n\n".join(selected)
        + "\n"
    )


@lru_cache(maxsize=1)
def krea2_examples_excerpt() -> str:
    """Krea's own guide, minus the sample images the model cannot see.

    The 19 worked prompts are the useful part -- they are exactly the register
    Krea 2 was trained on -- but the file interleaves them with `<img>` tags and
    `<br/>` spacers that would burn context for nothing.
    """
    content = load_guide("krea2")["content"]
    kept = [
        line for line in content.split("\n")
        if not line.strip().startswith(("<img", "<br"))
    ]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip() + "\n"
    if "```" in text or text.count("`") < 20:
        raise RuntimeError("Could not extract the Krea 2 example prompts from the official guide.")
    return text


@lru_cache(maxsize=1)
def anima_prompting_excerpt() -> str:
    """Only the prompting and limitation sections of the Anima model card.

    The card also covers installation, sampler settings, finetuning and
    licensing. None of that helps write a prompt, and all of it would displace
    the tag-order rules that do.
    """
    content = load_guide("anima")["content"]
    sections: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for line in content.split("\n"):
        if line.startswith("# "):
            if current is not None:
                sections[current] = "\n".join(lines).strip()
            current = line[2:].strip()
            lines = [line]
            continue
        if current is not None:
            lines.append(line)
    if current is not None:
        sections[current] = "\n".join(lines).strip()
    wanted = ("Prompting", "Limitations")
    missing = [name for name in wanted if not sections.get(name)]
    if missing:
        raise RuntimeError(f"Could not extract {missing} from the official Anima model card.")
    return "\n\n".join(sections[name] for name in wanted) + "\n"


def _html_text(fragment: str) -> str:
    import html

    text = re.sub(r"<br\s*/?>|</p>|</li>", "\n", fragment)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


@lru_cache(maxsize=1)
def illustrious_prompting_excerpt() -> str:
    """The card's Usage paragraph and its two worked prompt pairs, as text.

    The rest of the card is licence terms, safety policy and release notes, in
    HTML -- none of it helps write a tag list.
    """
    content = load_guide("illustrious")["content"]
    start = content.find("<strong>Usage:</strong>")
    end = content.find("</div>", start)
    if start < 0 or end < 0:
        raise RuntimeError("Could not extract the Usage section from the official Illustrious XL model card.")
    return "# Illustrious XL usage (official model card)\n\n" + _html_text(content[start:end]) + "\n"


@lru_cache(maxsize=1)
def pony_prompting_excerpt() -> str:
    """From "Important information" up to the credits, as text.

    That span holds the score_ template, the source_/rating_ tags and the
    clip-skip warning; the credits and training notes after it do not.
    """
    content = _html_text(load_guide("pony")["content"])
    start = content.find("Important information")
    end = content.find("Special thanks", start)
    if start < 0 or end < 0:
        raise RuntimeError("Could not extract the prompting rules from the official Pony Diffusion V6 description.")
    return "# Pony Diffusion V6 XL prompting (official model description)\n\n" + content[start:end].strip() + "\n"


def _markdown_section(content: str, heading: str, stop: str) -> str:
    start = content.find(heading)
    end = content.find(stop, start + len(heading)) if start >= 0 else -1
    if start < 0:
        raise RuntimeError(f"Could not find {heading!r} in a vendored guide.")
    return content[start:end if end >= 0 else None].strip()


@lru_cache(maxsize=1)
def qwen_image_prompting_excerpt() -> str:
    """The English SYSTEM_PROMPT string out of Qwen's prompt_utils_2512.py.

    The file is Python (an API wrapper plus the English and Chinese prompts);
    only the English rewriting instructions are guidance for writing a prompt.
    """
    content = load_guide("qwen_image")["content"]
    body = content.split("def polish_prompt_en", 1)[-1]
    start = body.find("SYSTEM_PROMPT = '''")
    end = body.find("'''", start + 20)
    if start < 0 or end < 0:
        raise RuntimeError("Could not extract the English system prompt from the official Qwen-Image prompt utils.")
    return body[start + len("SYSTEM_PROMPT = '''"):end].strip() + "\n"


@lru_cache(maxsize=1)
def flux3_prompting_excerpt() -> str:
    """The text-to-image upsampler prompt BFL publishes for use with an outside VLM."""
    import json

    data = json.loads(load_guide("flux3")["content"])
    text = data.get("t2i") if isinstance(data, dict) else None
    if not isinstance(text, str) or len(text) < 500:
        raise RuntimeError("Could not extract the t2i upsampler prompt from the official FLUX 3 prompts.")
    return text.strip() + "\n"


@lru_cache(maxsize=1)
def moss_image_prompting_excerpt() -> str:
    """Prompt Guidelines, without the experimental JSON layout format."""
    content = load_guide("moss_image")["content"]
    return _markdown_section(content, "## Prompt Guidelines", "### Layout Prompts") + "\n"


@lru_cache(maxsize=1)
def moss_image_caption_excerpt() -> str:
    """The enhancer's Caption deliverable: cohesive prose, visible text quoted exactly."""
    content = load_guide("moss_image_enhancer")["content"]
    return _markdown_section(content, "## Caption", "## Layout") + "\n"


def guide_excerpt(guide_id: str) -> str:
    """A guide's text as it is sent to the model: its declared excerpt, or the whole file."""
    name = GUIDES[guide_id].excerpt
    return globals()[name]() if name else load_guide(guide_id)["content"]


def guide_catalog() -> list[dict[str, str | list[str]]]:
    """Every vendored guide, with the modes written from it.

    Modes come from the target registry rather than `MODE_GUIDES`, which can only
    credit one guide per mode -- Krea 2 is written from two, and its expansion
    instructions were listed as belonging to no mode at all.
    """
    from .targets import all_modes, guide_ids_for_mode

    credited: dict[str, list[str]] = {guide_id: [] for guide_id in GUIDES}
    for mode in all_modes():
        for guide_id in guide_ids_for_mode(mode):
            credited.setdefault(guide_id, []).append(mode)
    return [
        {key: value for key, value in load_guide(guide_id).items() if key != "content"}
        | {"modes": credited.get(guide_id, [])}
        for guide_id in GUIDES
    ]
