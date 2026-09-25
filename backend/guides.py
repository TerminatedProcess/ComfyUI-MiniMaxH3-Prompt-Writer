from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path


GUIDES_DIR = Path(__file__).resolve().parent.parent / "guides"
SOURCE_REVISION = "bfc8ed0353f5a9733be73e6b2c98ec0948195b86"
SOURCE_ROOT = f"https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/{SOURCE_REVISION}/docs"
KREA2_REVISION = "db3984fbc6e13b34c0064990fc2d95ac64d00058"
KREA2_SOURCE_ROOT = f"https://github.com/krea-ai/krea-2/blob/{KREA2_REVISION}/docs"
ANIMA_REVISION = "f973fc41ec7545364ac9776c2440285f43ff2a30"
ANIMA_SOURCE_ROOT = f"https://huggingface.co/circlestone-labs/Anima/blob/{ANIMA_REVISION}"


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
