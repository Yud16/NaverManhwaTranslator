import html as html_lib
import json
import logging
import os
import re
import tempfile
import threading
import time
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from google.genai import types
from PIL import Image
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
import io

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("comictranslator")


class Settings(BaseSettings):
    google_api_key: str
    gemini_model: str = "gemini-flash-lite-latest"
    host: str = "127.0.0.1"
    port: int = 8000
    # Minimum gap enforced between outgoing Gemini calls. The free tier's
    # requests-per-minute cap is easy to blow through when panels are
    # translated back-to-back, so pace calls proactively instead of only
    # reacting to 429s after the fact.
    gemini_min_interval_seconds: float = 7.0
    gemini_max_retries: int = 6
    # How many panel images go into a single Gemini call. The free tier's
    # daily cap is on *requests*, not tokens/images-per-request, so batching
    # panels together divides the effective quota cost by roughly this
    # number. Higher = fewer calls, but a bigger blast radius if one call
    # fails, and more images for the model to keep straight at once.
    gemini_batch_size: int = 5
    # How many detected text lines go into a single Gemini translation call
    # (used when the detector isn't Gemini but the engine is — e.g. the
    # PaddleOCR-detector + Gemini-engine combo). Text lines are far lighter
    # than whole images, so this can safely be much larger than
    # gemini_batch_size: confirmed the naive one-call-per-box approach cost
    # 100+ throttled Gemini calls (10+ minutes of pure pacing) on a full
    # episode; batching lines like this cuts that to a handful of calls.
    gemini_text_batch_size: int = 30
    # Optional: only needed if the "azure" translation engine is selected.
    azure_translator_key: str = ""
    azure_translator_region: str = ""
    # Optional: only needed if the "deepl" translation engine is selected.
    deepl_api_key: str = ""
    # PaddleOCR detections below this confidence are dropped rather than
    # shown. Empirically this is what separates real printed dialogue
    # (94%+ in testing) from stylized SFX lettering it garbles (<70%).
    paddle_min_confidence: float = 0.85

    class Config:
        env_file = ".env"


@lru_cache
def get_settings() -> Settings:
    return Settings()


# Only these CDN hosts may be fetched server-side, to stop this local proxy
# from being abused as an open image fetcher.
ALLOWED_IMAGE_HOST_SUFFIXES = (".pstatic.net",)

app = FastAPI(title="Comic Translator Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^chrome-extension://.*",
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Cache of finished translations (image_url::engine::glossary -> response
# dict) and raw detections (image_url -> TranslationResult), persisted to
# disk (see load_cache/save_cache below) so a backend restart doesn't throw
# away already-paid-for Gemini calls. Episode images never change once
# published, so entries never need invalidating within or across runs.
CACHE_FILE = Path(__file__).parent / "cache.json"
_cache: dict[str, dict] = {}


# Gemini is specifically aligned to emit spatial/grounding coordinates as
# [ymin, xmin, ymax, xmax] normalized to a 0-1000 scale (Google's own
# object-detection convention) — asking it to freehand actual pixel x/y/w/h
# measurements produced noticeably worse box placement in testing.
class RawBox(BaseModel):
    ymin: int = Field(description="Top edge, 0-1000 (fraction of image height x1000)")
    xmin: int = Field(description="Left edge, 0-1000 (fraction of image width x1000)")
    ymax: int = Field(description="Bottom edge, 0-1000 (fraction of image height x1000)")
    xmax: int = Field(description="Right edge, 0-1000 (fraction of image width x1000)")
    korean: str = Field(description="The original Korean text in this region")
    english: str = Field(description="Natural English translation preserving tone")


class TranslationResult(BaseModel):
    boxes: list[RawBox]


class BatchResult(BaseModel):
    images: list[TranslationResult] = Field(
        description="Exactly one entry per input image, in the same order the images were given"
    )


class TextBox(BaseModel):
    x: int
    y: int
    w: int
    h: int
    korean: str
    english: str


def denormalize_box(box: RawBox, img_w: int, img_h: int) -> TextBox:
    x = round(box.xmin / 1000 * img_w)
    y = round(box.ymin / 1000 * img_h)
    w = round((box.xmax - box.xmin) / 1000 * img_w)
    h = round((box.ymax - box.ymin) / 1000 * img_h)
    return TextBox(x=x, y=y, w=w, h=h, korean=box.korean, english=box.english)


class GlossaryEntry(BaseModel):
    korean: str
    english: str


class TranslateRequest(BaseModel):
    image_url: str
    glossary: list[GlossaryEntry] = []
    force: bool = False
    engine: str = "gemini"  # "gemini" | "azure" | "deepl" | "nllb"
    detector: str = "paddleocr"  # "paddleocr" | "gemini"


class RetranslateRequest(BaseModel):
    korean: str
    glossary: list[GlossaryEntry] = []
    engine: str = "gemini"


class TranslateBatchRequest(BaseModel):
    image_urls: list[str]
    glossary: list[GlossaryEntry] = []
    force: bool = False
    engine: str = "gemini"
    detector: str = "paddleocr"


class RegionCrop(BaseModel):
    image_url: str
    x: int
    y: int
    w: int
    h: int


class TranslateRegionRequest(BaseModel):
    # More than one crop covers a manual selection that spans the boundary
    # between two adjacent panel images (in page order) — they're composited
    # into one image before detection, same trick as the automated boundary
    # fix, just user-triggered instead of run for every boundary.
    crops: list[RegionCrop]
    glossary: list[GlossaryEntry] = []
    engine: str = "gemini"
    detector: str = "paddleocr"


REGION_INSTRUCTIONS = """Find every region containing Korean text meant to be read: dialogue in speech
or thought bubbles, narration boxes, and sound-effect (SFX) text drawn into
the art. For each region return:
- a tight bounding box as ymin, xmin, ymax, xmax, each an integer from 0 to
  1000 representing the position as a fraction of THAT IMAGE's own
  height/width (i.e. multiply the fractional position by 1000 and round to
  an integer). The box should tightly hug just the glyphs themselves, not
  the whole speech bubble shape around them.
- the original Korean text
- a natural, tone-preserving English translation (shouting stays shouting,
  casual speech stays casual)

Ignore watermarks, logos, page numbers, and UI chrome. If a panel has no
text, its entry should have an empty boxes list. Do not merge separate
bubbles into one box."""

PROMPT = f"""You are analyzing one panel image from a Korean webtoon (manhwa).

{REGION_INSTRUCTIONS}"""


def build_batch_prompt(count: int) -> str:
    return f"""You are analyzing {count} panel images from a Korean webtoon (manhwa), given
in order and each preceded by a label like "=== Image 3 ===".

{REGION_INSTRUCTIONS}

Return exactly {count} entries in the `images` array, in the same order as
the input images — images[0] for "Image 1", images[1] for "Image 2", and so
on. Every image gets an entry even if its boxes list ends up empty. Treat
each image completely independently: coordinates are always relative to
that single image, never the batch as a whole, and text/context from one
image must not bleed into another's translation."""


def build_glossary_instructions(glossary: list[GlossaryEntry]) -> str:
    if not glossary:
        return ""
    lines = "\n".join(f'- "{e.korean}" -> "{e.english}"' for e in glossary)
    return (
        "\n\nThe reader has fixed the English form of these recurring names/"
        "terms. Whenever the Korean text contains one, use exactly the given "
        f"English form for that part (don't retranslate or reword it):\n{lines}"
    )


def glossary_cache_key(glossary: list[GlossaryEntry]) -> str:
    pairs = sorted((e.korean, e.english) for e in glossary)
    return "|".join(f"{k}={v}" for k, v in pairs)


def fetch_image(image_url: str) -> tuple[bytes, Image.Image]:
    host = urlparse(image_url).hostname or ""
    if not any(host.endswith(suffix) for suffix in ALLOWED_IMAGE_HOST_SUFFIXES):
        raise HTTPException(status_code=400, detail=f"Image host not allowed: {host}")

    resp = requests.get(
        image_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://comic.naver.com/",
        },
        timeout=15,
    )
    resp.raise_for_status()
    image_bytes = resp.content
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    return image_bytes, img


def sample_border_color(img: Image.Image, box: TextBox) -> str:
    """Approximate a text region's background color by averaging its outer
    border pixels, which for a speech bubble is almost always background
    rather than glyph strokes."""
    x = max(0, min(box.x, img.width - 1))
    y = max(0, min(box.y, img.height - 1))
    w = max(1, min(box.w, img.width - x))
    h = max(1, min(box.h, img.height - y))
    crop = img.crop((x, y, x + w, y + h))
    cw, ch = crop.size
    px = crop.load()

    samples = []
    step_x = max(1, cw // 20)
    step_y = max(1, ch // 20)
    for i in range(0, cw, step_x):
        samples.append(px[i, 0])
        samples.append(px[i, ch - 1])
    for j in range(0, ch, step_y):
        samples.append(px[0, j])
        samples.append(px[cw - 1, j])

    if not samples:
        return "#FFFFFF"
    r = sum(s[0] for s in samples) // len(samples)
    g = sum(s[1] for s in samples) // len(samples)
    b = sum(s[2] for s in samples) // len(samples)
    return f"#{r:02X}{g:02X}{b:02X}"


# Serializes and paces outgoing Gemini calls across requests so the free
# tier's RPM cap is respected proactively rather than discovered via 429s.
_throttle_lock = threading.Lock()
_last_call_at = 0.0


def _throttle(min_interval: float) -> None:
    global _last_call_at
    with _throttle_lock:
        wait = _last_call_at + min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call_at = time.monotonic()


def _is_rate_limit_error(message: str) -> bool:
    return "429" in message or "RESOURCE_EXHAUSTED" in message


def _is_daily_quota_exhausted(message: str) -> bool:
    # Google's quotaId distinguishes "PerDay" (won't recover until tomorrow —
    # retrying is pure wasted time) from "PerMinute"/burst limits (genuinely
    # worth backing off for). Same "429 RESOURCE_EXHAUSTED" text either way,
    # so this is the only reliable way to tell them apart.
    return "PerDay" in message


def _is_transient_server_error(message: str) -> bool:
    return any(code in message for code in ("500", "503", "INTERNAL", "UNAVAILABLE"))


def _run_gemini(settings: Settings, contents: list, response_schema: type[BaseModel]):
    client = genai.Client(api_key=settings.google_api_key)
    last_message = ""

    for attempt in range(settings.gemini_max_retries):
        _throttle(settings.gemini_min_interval_seconds)
        try:
            response = client.models.generate_content(
                model=settings.gemini_model,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=response_schema,
                    temperature=0.2,
                ),
            )
            return response_schema.model_validate_json(response.text)
        except Exception as exc:  # google-genai raises its own error types
            last_message = str(exc)

            if _is_daily_quota_exhausted(last_message):
                logger.warning("Gemini daily quota exhausted, failing immediately (no point retrying): %s", last_message[:200])
                raise HTTPException(
                    status_code=429,
                    detail="Gemini's free daily quota is used up for today — try again after it resets, or switch engines.",
                )

            if _is_rate_limit_error(last_message) or _is_transient_server_error(last_message):
                backoff = min(60.0, 5.0 * (2**attempt))
                logger.warning(
                    "Gemini call failed (attempt %d/%d), backing off %.0fs: %s",
                    attempt + 1,
                    settings.gemini_max_retries,
                    backoff,
                    last_message[:200],
                )
                time.sleep(backoff)
                continue
            logger.exception("Gemini call failed")
            raise HTTPException(status_code=502, detail=f"Gemini request failed: {last_message}")

    status_code = 429 if _is_rate_limit_error(last_message) else 502
    raise HTTPException(status_code=status_code, detail=f"Gemini call failed after retries: {last_message}")


def call_gemini(settings: Settings, image_bytes: bytes, glossary: list[GlossaryEntry]) -> TranslationResult:
    prompt = PROMPT + build_glossary_instructions(glossary)
    contents = [prompt, types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")]
    return _run_gemini(settings, contents, TranslationResult)


def call_gemini_batch(
    settings: Settings, images_bytes: list[bytes], glossary: list[GlossaryEntry]
) -> list[TranslationResult]:
    """Detects+translates several panels in one Gemini call. The free tier's
    daily cap is on request count, not per-request size, so this is the
    main lever for reading more episodes/day on the same quota."""
    prompt = build_batch_prompt(len(images_bytes)) + build_glossary_instructions(glossary)
    contents: list = [prompt]
    for i, image_bytes in enumerate(images_bytes, start=1):
        contents.append(f"=== Image {i} ===")
        contents.append(types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"))

    result = _run_gemini(settings, contents, BatchResult)
    if len(result.images) != len(images_bytes):
        logger.warning(
            "Gemini batch returned %d results for %d images — padding/truncating to match",
            len(result.images),
            len(images_bytes),
        )
        # Rather than fail the whole batch over a count mismatch, keep
        # what lines up positionally and treat any missing images as
        # text-free — a caller can always force-retry an individual image.
        images = result.images[: len(images_bytes)]
        while len(images) < len(images_bytes):
            images.append(TranslationResult(boxes=[]))
        return images
    return result.images


def call_gemini_batch_chunked(
    settings: Settings, images_bytes: list[bytes], glossary: list[GlossaryEntry]
) -> list[TranslationResult]:
    """Splits into settings.gemini_batch_size-sized calls to call_gemini_batch.
    Callers may hand this an arbitrarily large list (e.g. a whole episode);
    this is what actually enforces the configured batch size rather than
    trusting each caller to chunk correctly."""
    results: list[TranslationResult] = []
    size = max(1, settings.gemini_batch_size)
    for start in range(0, len(images_bytes), size):
        results.extend(call_gemini_batch(settings, images_bytes[start : start + size], glossary))
    return results


class GeminiTranslateResult(BaseModel):
    english: str = Field(description="Natural English translation preserving tone")


RETRANSLATE_PROMPT = """Translate this single line of Korean webtoon (manhwa) dialogue into
natural, tone-preserving English (shouting stays shouting, casual speech
stays casual). Return only the translation."""


def call_gemini_text(settings: Settings, korean: str, glossary: list[GlossaryEntry]) -> str:
    prompt = (
        RETRANSLATE_PROMPT
        + build_glossary_instructions(glossary)
        + f'\n\nKorean text: "{korean}"'
    )
    result = _run_gemini(settings, [prompt], GeminiTranslateResult)
    return result.english


class TextBatchResult(BaseModel):
    translations: list[str] = Field(
        description="Exactly one English translation per input line, in the same order the lines were given"
    )


def build_text_batch_prompt(count: int) -> str:
    return f"""Translate the following {count} lines of Korean webtoon (manhwa) dialogue into
natural, tone-preserving English (shouting stays shouting, casual speech
stays casual). Each line is independent — they're detected text from
possibly different speech bubbles or even different panels, not one
continuous conversation — so translate each on its own rather than
inferring context from its neighbors.

Return exactly {count} entries in the `translations` array, in the same
order as the input lines — translations[0] for line 1, translations[1] for
line 2, and so on. Every line gets an entry."""


def call_gemini_text_batch(
    settings: Settings, korean_texts: list[str], glossary: list[GlossaryEntry]
) -> list[str]:
    """Translates several independent lines of Korean text in one Gemini
    call instead of one call per line. Used when the detector already found
    the text (PaddleOCR or Gemini-as-OCR-only) but Gemini is doing the
    translating — previously every detected box cost its own Gemini
    round-trip, each paced at least gemini_min_interval_seconds apart, which
    dominated total translate time far more than actual API latency did."""
    numbered = "\n".join(f'{i}. "{text}"' for i, text in enumerate(korean_texts, start=1))
    prompt = (
        build_text_batch_prompt(len(korean_texts))
        + build_glossary_instructions(glossary)
        + f"\n\nLines:\n{numbered}"
    )
    result = _run_gemini(settings, [prompt], TextBatchResult)
    if len(result.translations) != len(korean_texts):
        logger.warning(
            "Gemini text batch returned %d results for %d lines — padding/truncating to match",
            len(result.translations),
            len(korean_texts),
        )
        translations = result.translations[: len(korean_texts)]
        while len(translations) < len(korean_texts):
            translations.append("")
        return translations
    return result.translations


def call_gemini_text_batch_chunked(
    settings: Settings, korean_texts: list[str], glossary: list[GlossaryEntry]
) -> list[str]:
    """Splits into settings.gemini_text_batch_size-sized calls to
    call_gemini_text_batch. Callers may hand this every line from a whole
    episode at once; this is what actually enforces the configured chunk
    size rather than trusting each caller to chunk correctly."""
    translations: list[str] = []
    size = max(1, settings.gemini_text_batch_size)
    for start in range(0, len(korean_texts), size):
        translations.extend(call_gemini_text_batch(settings, korean_texts[start : start + size], glossary))
    return translations


@app.get("/health")
def health():
    settings = get_settings()
    return {"status": "ok", "model": settings.gemini_model}


# --- Alternate detectors ----------------------------------------------------
#
# Detection (finding text regions + reading the Korean) defaults to
# PaddleOCR — free, local, no API key. Tested against real panels: it reads
# printed dialogue very accurately (94%+ confidence, exact matches) but is
# unreliable on stylized SFX lettering, either missing it or confidently
# returning garbage. Rather than show wrong SFX translations, low-confidence
# results are just dropped — Gemini remains available as the detector when
# full SFX coverage matters more than saving quota.

_paddle_ocr = None
_paddle_lock = threading.Lock()


def get_paddle_ocr():
    global _paddle_ocr
    if _paddle_ocr is None:
        with _paddle_lock:
            if _paddle_ocr is None:
                from paddleocr import PaddleOCR

                # enable_mkldnn=False works around a confirmed
                # NotImplementedError crash in PaddlePaddle 3.3.1's oneDNN
                # backend during CPU inference on Windows — not optional.
                #
                # The rest is a measured ~10x speedup (28s -> ~3s/image on
                # CPU) with comparable accuracy on real panels:
                # - use_doc_orientation_classify/use_doc_unwarping/
                #   use_textline_orientation=False skip three submodules
                #   meant for photographed/scanned documents (rotation,
                #   page warp, tilted text lines) that a flat, upright,
                #   digitally-native webtoon panel never needs — and
                #   dropping them measurably *improved* one recognition
                #   in testing rather than hurting it.
                # - text_detection_model_name switches from PP-OCRv5's
                #   heavy "server" detector to its "mobile" sibling, which
                #   is where nearly all of the speedup actually came from.
                #   The recognition model must be specified explicitly
                #   alongside it — leaving it implicit silently swapped in
                #   a non-Korean default and detection came back empty.
                _paddle_ocr = PaddleOCR(
                    lang="korean",
                    enable_mkldnn=False,
                    use_doc_orientation_classify=False,
                    use_doc_unwarping=False,
                    use_textline_orientation=False,
                    text_detection_model_name="PP-OCRv5_mobile_det",
                    text_recognition_model_name="korean_PP-OCRv5_mobile_rec",
                )
    return _paddle_ocr


def group_text_lines(
    lines: list[tuple[float, float, float, float, str]],
) -> list[tuple[float, float, float, float, str]]:
    """Merges individual OCR text-line detections into paragraph-level
    groups when they're vertically stacked with overlapping horizontal
    extent and a small gap between them — the shape of wrapped dialogue
    within a single speech bubble.

    PaddleOCR (unlike Gemini) detects one printed line at a time with no
    concept of "these lines are one sentence" — confirmed directly: a
    3-line bubble came back as 3 separate boxes, each translated in
    isolation into disconnected fragments instead of one coherent
    sentence. This groups lines back into what a reader would recognize as
    one block of dialogue before translation ever sees them."""
    if not lines:
        return []

    ordered = sorted(lines, key=lambda b: (b[1], b[0]))  # top-to-bottom, then left-to-right

    groups: list[list[tuple[float, float, float, float, str]]] = [[ordered[0]]]
    for box in ordered[1:]:
        x1, y1, x2, y2, _text = box
        gx1, gy1, gx2, gy2, _ = groups[-1][-1]
        line_height = max(1.0, gy2 - gy1)
        gap = y1 - gy2
        x_overlap = min(x2, gx2) - max(x1, gx1)
        narrower_width = min(x2 - x1, gx2 - gx1)
        horizontally_aligned = narrower_width > 0 and x_overlap > 0.15 * narrower_width

        if horizontally_aligned and gap < 0.7 * line_height:
            groups[-1].append(box)
        else:
            groups.append([box])

    merged = []
    for group in groups:
        gx1 = min(b[0] for b in group)
        gy1 = min(b[1] for b in group)
        gx2 = max(b[2] for b in group)
        gy2 = max(b[3] for b in group)
        text = " ".join(b[4] for b in group)
        merged.append((gx1, gy1, gx2, gy2, text))
    return merged


_HANGUL_RE = re.compile(r"[가-힣]")  # Hangul syllables block — covers all precomposed Korean text


def _has_hangul(text: str) -> bool:
    return bool(_HANGUL_RE.search(text))


def _iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    area_a = (ax2 - ax1) * (ay2 - ay1)
    area_b = (bx2 - bx1) * (by2 - by1)
    return inter / max(1.0, area_a + area_b - inter)


def _dedupe_lines(
    lines: list[tuple[float, float, float, float, str, float]],
) -> list[tuple[float, float, float, float, str]]:
    """PaddleOCR's detector occasionally emits two overlapping boxes for the
    same physical text line — seen on panels with a busy screentone/hatching
    background, which appears to confuse its internal box suppression. Both
    get recognized as the exact same text and both survive into two
    overlapping, doubly-rendered translations. When two detections share
    identical text and substantial overlap, keep only the higher-confidence
    one."""
    kept: list[tuple[float, float, float, float, str, float]] = []
    for line in sorted(lines, key=lambda b: -b[5]):
        x1, y1, x2, y2, text, _score = line
        if any(text == k[4] and _iou((x1, y1, x2, y2), (k[0], k[1], k[2], k[3])) > 0.4 for k in kept):
            continue
        kept.append(line)
    return [(x1, y1, x2, y2, text) for x1, y1, x2, y2, text, _score in kept]


def paddleocr_detect(settings: Settings, image_bytes: bytes, img_w: int, img_h: int) -> TranslationResult:
    ocr = get_paddle_ocr()
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        f.write(image_bytes)
        tmp_path = f.name

    try:
        # Serialize inference calls — the underlying predictor isn't
        # verified safe for concurrent use from multiple request threads.
        with _paddle_lock:
            results = ocr.predict(tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    lines = []
    for res in results:
        texts = res.get("rec_texts", [])
        scores = res.get("rec_scores", [])
        rec_boxes = res.get("rec_boxes", [])
        for text, score, box in zip(texts, scores, rec_boxes):
            if score < settings.paddle_min_confidence or not text.strip():
                continue
            if not _has_hangul(text):
                # Stylized SFX lettering sometimes gets confidently misread
                # as simple Latin digits/letters (e.g. "00", "6", "OK") —
                # confidently enough to survive the confidence filter above.
                # Real Korean dialogue always contains at least one Hangul
                # character, so anything without one is essentially always
                # a misread of artwork rather than actual text.
                continue
            x1, y1, x2, y2 = (float(v) for v in box)
            lines.append((x1, y1, x2, y2, text, float(score)))

    boxes = []
    for x1, y1, x2, y2, text in group_text_lines(_dedupe_lines(lines)):
        boxes.append(
            RawBox(
                ymin=round(y1 / img_h * 1000),
                xmin=round(x1 / img_w * 1000),
                ymax=round(y2 / img_h * 1000),
                xmax=round(x2 / img_w * 1000),
                korean=text,
                english="",  # PaddleOCR only detects/reads — never translates
            )
        )
    return TranslationResult(boxes=boxes)


# --- Alternate translation engines -----------------------------------------
#
# What's switchable independently of the detector is which engine turns
# detected Korean text into English: Gemini's own translation (glossary-aware
# via the prompt, only available when Gemini is also the detector — otherwise
# it's just another per-line translator like the rest), a general cloud MT
# call (Azure, DeepL), or a free/offline local model (NLLB). Non-Gemini
# engines don't take instructions, so glossary terms are locked in with a
# placeholder swap around the call (Azure gets a first-party mechanism
# instead — see build_azure_dictionary_text).


def deepl_translate_text(text: str, settings: Settings) -> str:
    if not settings.deepl_api_key:
        raise HTTPException(
            status_code=400,
            detail="DeepL engine selected but DEEPL_API_KEY is not set in backend/.env",
        )
    # Free-tier keys are suffixed ":fx" and only work against the free API
    # host; paid keys use the standard host. This is the documented way to
    # tell them apart without a separate settings flag.
    host = "api-free.deepl.com" if settings.deepl_api_key.endswith(":fx") else "api.deepl.com"
    resp = requests.post(
        f"https://{host}/v2/translate",
        headers={"Authorization": f"DeepL-Auth-Key {settings.deepl_api_key}"},
        data={"text": text, "source_lang": "KO", "target_lang": "EN-US"},
        timeout=10,
    )
    if not resp.ok:
        raise HTTPException(status_code=502, detail=f"DeepL request failed: {resp.status_code} {resp.text[:200]}")
    data = resp.json()
    try:
        return data["translations"][0]["text"]
    except (KeyError, IndexError, TypeError):
        raise HTTPException(status_code=502, detail=f"Unexpected DeepL response shape: {data}")


def build_azure_dictionary_text(text: str, glossary: list[GlossaryEntry]) -> str:
    """Azure's "dynamic dictionary" markup is a first-party, officially
    supported way to lock proper-noun translations — more reliable than the
    placeholder-swap trick the other black-box engines need."""
    escaped = html_lib.escape(text, quote=False)
    for entry in glossary:
        kr = html_lib.escape(entry.korean, quote=False)
        en = html_lib.escape(entry.english, quote=True)
        if kr and kr in escaped:
            tag = f'<mstrans:dictionary translation="{en}">{kr}</mstrans:dictionary>'
            escaped = escaped.replace(kr, tag)
    return escaped


def azure_translate_text(text: str, glossary: list[GlossaryEntry], settings: Settings) -> str:
    if not settings.azure_translator_key:
        raise HTTPException(
            status_code=400,
            detail="Azure engine selected but AZURE_TRANSLATOR_KEY is not set in backend/.env",
        )

    use_dictionary = bool(glossary)
    body_text = build_azure_dictionary_text(text, glossary) if use_dictionary else text

    headers = {
        "Ocp-Apim-Subscription-Key": settings.azure_translator_key,
        "Content-Type": "application/json; charset=UTF-8",
    }
    if settings.azure_translator_region:
        # Azure's API wants the short region code (e.g. "westus2"), but the
        # portal displays — and people naturally copy — the friendly name
        # ("West US 2"). Normalize so either form works.
        headers["Ocp-Apim-Subscription-Region"] = settings.azure_translator_region.lower().replace(" ", "")

    resp = requests.post(
        "https://api.cognitive.microsofttranslator.com/translate",
        params={
            "api-version": "3.0",
            "from": "ko",
            "to": "en",
            "textType": "html" if use_dictionary else "plain",
        },
        headers=headers,
        json=[{"Text": body_text}],
        timeout=10,
    )
    if not resp.ok:
        raise HTTPException(status_code=502, detail=f"Azure Translator request failed: {resp.status_code} {resp.text[:200]}")
    data = resp.json()
    try:
        return data[0]["translations"][0]["text"]
    except (KeyError, IndexError, TypeError):
        raise HTTPException(status_code=502, detail=f"Unexpected Azure response shape: {data}")


# NLLB-200 (distilled 600M): an actual pretrained multilingual MT model, not
# a from-scratch training exercise — no API key, no per-call cost, no quota.
# Downloaded once from Hugging Face on first use (~2.4GB) and cached under
# ~/.cache/huggingface after that; translation itself needs no network once
# cached. Quality trades off against Gemini's (small local model vs. a
# frontier LLM) but is a real, actively-maintained model rather than the
# unweighted student project this replaces as an idea.
_nllb_model = None
_nllb_tokenizer = None
_nllb_lock = threading.Lock()


def get_nllb_model():
    # Loads the tokenizer/model directly instead of via pipeline("translation", ...):
    # the installed transformers version's pipeline task registry no longer
    # recognizes the "translation" task string at all, so the high-level
    # shortcut raises KeyError. AutoTokenizer/AutoModelForSeq2SeqLM + .generate()
    # is the stable, version-independent way to run NLLB.
    global _nllb_model, _nllb_tokenizer
    if _nllb_model is None:
        with _nllb_lock:
            if _nllb_model is None:
                try:
                    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
                except ImportError:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "NLLB engine selected but its dependencies aren't installed. "
                            "Run: pip install torch transformers sentencepiece"
                        ),
                    )
                model_name = "facebook/nllb-200-distilled-600M"
                _nllb_tokenizer = AutoTokenizer.from_pretrained(model_name, src_lang="kor_Hang")
                _nllb_model = AutoModelForSeq2SeqLM.from_pretrained(model_name)
    return _nllb_model, _nllb_tokenizer


def nllb_translate_text(text: str) -> str:
    model, tokenizer = get_nllb_model()
    inputs = tokenizer(text, return_tensors="pt")
    generated = model.generate(
        **inputs,
        forced_bos_token_id=tokenizer.convert_tokens_to_ids("eng_Latn"),
        max_new_tokens=512,
    )
    return tokenizer.batch_decode(generated, skip_special_tokens=True)[0]


def translate_with_glossary(text: str, glossary: list[GlossaryEntry], engine_fn) -> str:
    """Best-effort term-locking for engines that don't take instructions:
    swap each glossary term for a placeholder token before translating, then
    swap the placeholder back for the reader's fixed English form. Small MT
    models don't always preserve unfamiliar tokens perfectly, so this isn't
    guaranteed the way Gemini's prompt-based locking is, but it's the
    standard trick for term-locking a black-box translator."""
    working = text
    placeholders: dict[str, str] = {}
    for i, entry in enumerate(glossary):
        if entry.korean and entry.korean in working:
            token = f"@{i}@"
            working = working.replace(entry.korean, token)
            placeholders[token] = entry.english

    translated = engine_fn(working)

    for token, english in placeholders.items():
        translated = translated.replace(token, english)
    return translated


# Raw detection (Korean text + box coordinates) doesn't depend on glossary or
# translation engine, so it's cached separately and reused across engine/
# glossary switches — only the "gemini" engine path needs a fresh call when
# the glossary changes, since its translation is glossary-aware at call time.
# Kept as two separate dicts (rather than one keyed by detector) so the
# existing Gemini-detector cache/behavior stays untouched by adding a second
# detector option.
_ocr_cache: dict[str, TranslationResult] = {}
_paddle_cache: dict[str, TranslationResult] = {}

_cache_save_lock = threading.Lock()


def load_cache() -> None:
    if not CACHE_FILE.exists():
        return
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Couldn't read %s, starting with an empty cache", CACHE_FILE)
        return
    _cache.update(data.get("translate_cache", {}))
    for url, raw in data.get("ocr_cache", {}).items():
        try:
            _ocr_cache[url] = TranslationResult.model_validate(raw)
        except Exception:
            continue  # skip anything that doesn't match the current schema
    for url, raw in data.get("paddle_cache", {}).items():
        try:
            _paddle_cache[url] = TranslationResult.model_validate(raw)
        except Exception:
            continue
    logger.info(
        "Loaded cache: %d translation(s), %d Gemini detection(s), %d PaddleOCR detection(s)",
        len(_cache),
        len(_ocr_cache),
        len(_paddle_cache),
    )


def save_cache() -> None:
    # Serialize under a lock and write atomically (temp file + replace) so a
    # crash or concurrent request mid-write can't corrupt the cache file.
    with _cache_save_lock:
        data = {
            "translate_cache": _cache,
            "ocr_cache": {url: result.model_dump() for url, result in _ocr_cache.items()},
            "paddle_cache": {url: result.model_dump() for url, result in _paddle_cache.items()},
        }
        tmp_path = CACHE_FILE.with_suffix(".tmp")
        try:
            tmp_path.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp_path, CACHE_FILE)
        except OSError:
            logger.exception("Failed to save cache to %s", CACHE_FILE)


load_cache()


def get_ocr_boxes(settings: Settings, image_url: str, image_bytes: bytes, force: bool) -> TranslationResult:
    if not force and image_url in _ocr_cache:
        return _ocr_cache[image_url]
    result = call_gemini(settings, image_bytes, glossary=[])
    _ocr_cache[image_url] = result
    return result


def get_ocr_boxes_batch(
    settings: Settings, image_urls: list[str], images_bytes: list[bytes], force: bool
) -> list[TranslationResult]:
    """Batch version of get_ocr_boxes: only the images actually missing from
    the cache (or all of them, if forced) go into a single Gemini call —
    already-cached ones cost nothing."""
    results: list[TranslationResult | None] = [None] * len(image_urls)
    missing_indices = []

    for i, url in enumerate(image_urls):
        if not force and url in _ocr_cache:
            results[i] = _ocr_cache[url]
        else:
            missing_indices.append(i)

    if missing_indices:
        fetched = call_gemini_batch_chunked(settings, [images_bytes[i] for i in missing_indices], glossary=[])
        for i, result in zip(missing_indices, fetched):
            results[i] = result
            _ocr_cache[image_urls[i]] = result

    return results


def get_paddle_boxes(
    settings: Settings, image_url: str, image_bytes: bytes, img: Image.Image, force: bool
) -> TranslationResult:
    if not force and image_url in _paddle_cache:
        return _paddle_cache[image_url]
    result = paddleocr_detect(settings, image_bytes, img.width, img.height)
    _paddle_cache[image_url] = result
    return result


def get_paddle_boxes_batch(
    settings: Settings,
    image_urls: list[str],
    images_bytes: list[bytes],
    imgs: list[Image.Image],
    force: bool,
) -> list[TranslationResult]:
    # No Gemini-style batching needed — PaddleOCR runs locally with no
    # per-request quota to conserve, so each image is just processed in turn.
    return [
        get_paddle_boxes(settings, url, image_bytes, img, force)
        for url, image_bytes, img in zip(image_urls, images_bytes, imgs)
    ]


def clean_text(text: str) -> str:
    """Gemini sometimes emits literal "\\n" escape sequences (or real
    newlines) inside translated text. Bubbles wrap on their own via CSS, so
    collapse any of that into plain spaces rather than showing a stray
    backslash-n or relying on line breaks the layout doesn't expect."""
    return " ".join(text.replace("\\n", " ").replace("\\r", " ").split())


def clamp_box(box: TextBox, img_w: int, img_h: int) -> TextBox | None:
    """Gemini's pixel coordinates occasionally overshoot the image (seen in
    testing: boxes extending past the right/bottom edge). Clamp to bounds
    and drop anything that ends up with no visible area."""
    x = max(0, min(box.x, img_w - 1))
    y = max(0, min(box.y, img_h - 1))
    w = max(0, min(box.w, img_w - x))
    h = max(0, min(box.h, img_h - y))
    if w < 4 or h < 4:
        return None
    return TextBox(
        x=x,
        y=y,
        w=w,
        h=h,
        korean=clean_text(box.korean),
        english=clean_text(box.english),
    )


def clamp_boxes(img: Image.Image, result: TranslationResult) -> list[TextBox]:
    """Denormalizes + clamps every raw detection for one image, dropping any
    that end up with no visible area. Split out from build_payload so a
    caller translating in a cross-image batch (see
    call_gemini_text_batch_chunked) can collect every image's boxes before
    translating any of them, instead of one image — and one box — at a
    time."""
    boxes = []
    for raw_box in result.boxes:
        box = clamp_box(denormalize_box(raw_box, img.width, img.height), img.width, img.height)
        if box is not None:
            boxes.append(box)
    return boxes


def build_payload(
    img: Image.Image,
    boxes: list[TextBox],
    engine: str,
    glossary: list[GlossaryEntry],
    settings: Settings,
    detector: str,
    gemini_translations: list[str] | None = None,
) -> dict:
    """Turns one image's already-clamped boxes into the final API response
    shape, translating each via the selected engine. When
    detector == engine == "gemini", the translation was already produced
    together with detection (glossary-aware, via the prompt) and `box.english`
    is trusted as-is. When engine == "gemini" with a different detector,
    gemini_translations (pre-computed via a batched call covering possibly
    several images at once) is used if given, falling back to one Gemini
    call per box otherwise. Every other engine still translates box-by-box —
    those calls aren't throttled the way Gemini's are, so batching them
    hasn't been worth the same complexity."""
    out_boxes = []
    for i, box in enumerate(boxes):
        if engine == "gemini":
            if detector == "gemini":
                english = box.english
            elif gemini_translations is not None:
                english = gemini_translations[i]
            else:
                english = call_gemini_text(settings, box.korean, glossary)
        elif engine == "azure":
            english = azure_translate_text(box.korean, glossary, settings)
        elif engine == "deepl":
            english = translate_with_glossary(box.korean, glossary, lambda t: deepl_translate_text(t, settings))
        else:  # nllb
            english = translate_with_glossary(box.korean, glossary, nllb_translate_text)

        out_boxes.append(
            {
                "x": box.x,
                "y": box.y,
                "w": box.w,
                "h": box.h,
                "korean": box.korean,
                "english": clean_text(english),
                "color": sample_border_color(img, box),
            }
        )

    return {"width": img.width, "height": img.height, "boxes": out_boxes}


def _validate_engine_detector(engine: str, detector: str) -> None:
    if engine not in ("gemini", "azure", "deepl", "nllb"):
        raise HTTPException(status_code=400, detail=f"Unknown engine: {engine}")
    if detector not in ("gemini", "paddleocr"):
        raise HTTPException(status_code=400, detail=f"Unknown detector: {detector}")


def _detect(settings: Settings, detector: str, engine: str, image_url: str, image_bytes: bytes, img: Image.Image, glossary: list[GlossaryEntry], force: bool) -> TranslationResult:
    if detector == "paddleocr":
        return get_paddle_boxes(settings, image_url, image_bytes, img, force)
    # detector == "gemini"
    if engine == "gemini":
        return call_gemini(settings, image_bytes, glossary)
    return get_ocr_boxes(settings, image_url, image_bytes, force)


@app.post("/translate")
def translate(req: TranslateRequest):
    _validate_engine_detector(req.engine, req.detector)

    cache_key = f"{req.image_url}::{req.detector}::{req.engine}::{glossary_cache_key(req.glossary)}"
    if not req.force and cache_key in _cache:
        return _cache[cache_key]

    settings = get_settings()
    image_bytes, img = fetch_image(req.image_url)

    result = _detect(settings, req.detector, req.engine, req.image_url, image_bytes, img, req.glossary, req.force)

    boxes = clamp_boxes(img, result)
    payload = build_payload(img, boxes, req.engine, req.glossary, settings, req.detector)
    _cache[cache_key] = payload
    save_cache()
    return payload


@app.post("/translate_batch")
def translate_batch(req: TranslateBatchRequest):
    _validate_engine_detector(req.engine, req.detector)
    if len(req.image_urls) > 25:
        raise HTTPException(status_code=400, detail="Batch too large (max 25 images)")

    settings = get_settings()

    # Anything already fully cached for this exact detector+engine+glossary
    # needs no work at all — only genuinely new content goes into detection.
    cache_keys = [
        f"{url}::{req.detector}::{req.engine}::{glossary_cache_key(req.glossary)}" for url in req.image_urls
    ]
    payloads: list[dict | None] = [None] * len(req.image_urls)
    pending_indices = []
    for i, key in enumerate(cache_keys):
        if not req.force and key in _cache:
            payloads[i] = _cache[key]
        else:
            pending_indices.append(i)

    if pending_indices:
        pending_urls = [req.image_urls[i] for i in pending_indices]
        fetched = [fetch_image(url) for url in pending_urls]
        images_bytes = [b for b, _ in fetched]
        imgs = [im for _, im in fetched]

        if req.detector == "paddleocr":
            results = get_paddle_boxes_batch(settings, pending_urls, images_bytes, imgs, req.force)
        elif req.engine == "gemini":
            results = call_gemini_batch_chunked(settings, images_bytes, req.glossary)
        else:
            results = get_ocr_boxes_batch(settings, pending_urls, images_bytes, req.force)

        # Clamp every image's raw detections up front so translation below
        # can be batched across the whole request instead of per-image.
        per_image_boxes = [clamp_boxes(img, result) for img, result in zip(imgs, results)]

        gemini_translations = None
        if req.engine == "gemini" and req.detector != "gemini":
            # One (or a few, chunked) Gemini call for every detected line in
            # this whole request instead of one throttled call per box —
            # see call_gemini_text_batch_chunked.
            flat_korean = [box.korean for boxes in per_image_boxes for box in boxes]
            if flat_korean:
                gemini_translations = call_gemini_text_batch_chunked(settings, flat_korean, req.glossary)

        offset = 0
        for idx, img, boxes in zip(pending_indices, imgs, per_image_boxes):
            image_translations = None
            if gemini_translations is not None:
                image_translations = gemini_translations[offset : offset + len(boxes)]
                offset += len(boxes)
            payload = build_payload(img, boxes, req.engine, req.glossary, settings, req.detector, image_translations)
            payloads[idx] = payload
            _cache[cache_keys[idx]] = payload

        save_cache()

    return {"results": payloads}


def translate_text_via_engine(korean: str, glossary: list[GlossaryEntry], engine: str, settings: Settings) -> str:
    if engine == "gemini":
        return call_gemini_text(settings, korean, glossary)
    elif engine == "azure":
        return azure_translate_text(korean, glossary, settings)
    elif engine == "deepl":
        return translate_with_glossary(korean, glossary, lambda t: deepl_translate_text(t, settings))
    else:  # nllb
        return translate_with_glossary(korean, glossary, nllb_translate_text)


@app.post("/retranslate")
def retranslate(req: RetranslateRequest):
    if req.engine not in ("gemini", "azure", "deepl", "nllb"):
        raise HTTPException(status_code=400, detail=f"Unknown engine: {req.engine}")

    settings = get_settings()
    english = translate_text_via_engine(req.korean, req.glossary, req.engine, settings)
    return {"english": clean_text(english)}


@app.post("/translate_region")
def translate_region(req: TranslateRegionRequest):
    _validate_engine_detector(req.engine, req.detector)
    if not req.crops:
        raise HTTPException(status_code=400, detail="No crop regions given")
    if len(req.crops) > 5:
        raise HTTPException(status_code=400, detail="Too many crop regions (max 5)")

    settings = get_settings()

    cropped = []
    for c in req.crops:
        _, img = fetch_image(c.image_url)
        x = max(0, min(c.x, img.width - 1))
        y = max(0, min(c.y, img.height - 1))
        w = max(1, min(c.w, img.width - x))
        h = max(1, min(c.h, img.height - y))
        cropped.append(img.crop((x, y, x + w, y + h)))

    if len(cropped) == 1:
        composite = cropped[0]
    else:
        # Selection spans an image boundary — stack the crops in the order
        # given (page order) into one image so a line split across the
        # boundary is read whole instead of as two cropped fragments.
        width = max(c.width for c in cropped)
        total_height = sum(c.height for c in cropped)
        composite = Image.new("RGB", (width, total_height), "white")
        y_off = 0
        for c in cropped:
            composite.paste(c, (0, y_off))
            y_off += c.height

    buf = io.BytesIO()
    composite.save(buf, format="JPEG", quality=92)
    composite_bytes = buf.getvalue()

    if req.detector == "gemini":
        result = call_gemini(settings, composite_bytes, [])
    else:
        result = paddleocr_detect(settings, composite_bytes, composite.width, composite.height)

    # Treat the whole manual selection as one translation unit rather than
    # rendering possibly-multiple sub-boxes — the user already delimited
    # exactly the area they care about, and the result renders as a single
    # floating overlay at that same selection rect regardless.
    korean = " ".join(clean_text(b.korean) for b in result.boxes if clean_text(b.korean))
    if not korean:
        return {"korean": "", "english": ""}

    english = translate_text_via_engine(korean, req.glossary, req.engine, settings)
    return {"korean": korean, "english": clean_text(english)}


if __name__ == "__main__":
    import sys

    import uvicorn
    from pydantic import ValidationError

    try:
        settings = get_settings()
    except ValidationError:
        # A friend running this for the first time sees this instead of a
        # raw pydantic traceback if .env is missing or GOOGLE_API_KEY isn't
        # set — setup.bat/setup_env.py should have already handled this, but
        # main.py can also be run directly.
        print()
        print("backend/.env is missing or doesn't have GOOGLE_API_KEY set.")
        print("Run setup.bat first, or copy .env.example to .env and add a")
        print("Gemini API key (free at https://aistudio.google.com/apikey).")
        print()
        sys.exit(1)
    uvicorn.run("main:app", host=settings.host, port=settings.port)
