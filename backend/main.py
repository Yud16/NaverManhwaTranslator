import html as html_lib
import json
import logging
import os
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
    # Optional: only needed if the "papago" translation engine is selected.
    naver_client_id: str = ""
    naver_client_secret: str = ""
    # Optional: only needed if the "azure" translation engine is selected.
    azure_translator_key: str = ""
    azure_translator_region: str = ""

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
    is_sfx: bool = Field(description="True for sound-effect/onomatopoeia text drawn into the art, false for actual dialogue/narration meant to be read as language")


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
    is_sfx: bool


def denormalize_box(box: RawBox, img_w: int, img_h: int) -> TextBox:
    x = round(box.xmin / 1000 * img_w)
    y = round(box.ymin / 1000 * img_h)
    w = round((box.xmax - box.xmin) / 1000 * img_w)
    h = round((box.ymax - box.ymin) / 1000 * img_h)
    return TextBox(x=x, y=y, w=w, h=h, korean=box.korean, english=box.english, is_sfx=box.is_sfx)


class GlossaryEntry(BaseModel):
    korean: str
    english: str


class TranslateRequest(BaseModel):
    image_url: str
    glossary: list[GlossaryEntry] = []
    force: bool = False
    engine: str = "gemini"  # "gemini" | "papago" | "argos"


class RetranslateRequest(BaseModel):
    korean: str
    glossary: list[GlossaryEntry] = []
    engine: str = "gemini"


class TranslateBatchRequest(BaseModel):
    image_urls: list[str]
    glossary: list[GlossaryEntry] = []
    force: bool = False
    engine: str = "gemini"


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
- is_sfx: true if this is a sound effect/onomatopoeia drawn into the art
  (impact sounds, footsteps, ambient noise, etc.), false for anything meant
  to be read as actual dialogue or narration

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


@app.get("/health")
def health():
    settings = get_settings()
    return {"status": "ok", "model": settings.gemini_model}


# --- Alternate translation engines -----------------------------------------
#
# Detection (finding text regions + reading the Korean) always goes through
# Gemini — free local detectors tried for this project weren't reliable
# enough on Korean. What's switchable is which engine turns that Korean text
# into English: Gemini's own translation (glossary-aware via the prompt), or
# a cheap/free local (Argos) or Korean-specialized (Papago) MT call applied
# to the same detected text. Non-Gemini engines don't take instructions, so
# glossary terms are locked in with a placeholder swap around the call.

_argos_ready = False
_argos_lock = threading.Lock()


def ensure_argos_ready() -> None:
    global _argos_ready
    if _argos_ready:
        return
    with _argos_lock:
        if _argos_ready:
            return
        import argostranslate.package

        installed = argostranslate.package.get_installed_packages()
        if not any(p.from_code == "ko" and p.to_code == "en" for p in installed):
            argostranslate.package.update_package_index()
            available = argostranslate.package.get_available_packages()
            pkg = next((p for p in available if p.from_code == "ko" and p.to_code == "en"), None)
            if pkg is None:
                raise HTTPException(status_code=502, detail="Argos ko->en package is not available for download")
            argostranslate.package.install_from_path(pkg.download())
        _argos_ready = True


def argos_translate_text(text: str) -> str:
    import argostranslate.translate

    ensure_argos_ready()
    return argostranslate.translate.translate(text, "ko", "en")


def papago_translate_text(text: str, settings: Settings) -> str:
    if not settings.naver_client_id or not settings.naver_client_secret:
        raise HTTPException(
            status_code=400,
            detail="Papago engine selected but NAVER_CLIENT_ID/NAVER_CLIENT_SECRET are not set in backend/.env",
        )
    resp = requests.post(
        "https://papago.apigw.ntruss.com/nmt/v1/translation",
        headers={
            "x-ncp-apigw-api-key-id": settings.naver_client_id,
            "x-ncp-apigw-api-key": settings.naver_client_secret,
        },
        data={"source": "ko", "target": "en", "text": text},
        timeout=10,
    )
    if not resp.ok:
        raise HTTPException(status_code=502, detail=f"Papago request failed: {resp.status_code} {resp.text[:200]}")
    data = resp.json()
    try:
        return data["message"]["result"]["translatedText"]
    except (KeyError, TypeError):
        raise HTTPException(status_code=502, detail=f"Unexpected Papago response shape: {data}")


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
_ocr_cache: dict[str, TranslationResult] = {}

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
    logger.info("Loaded cache: %d translation(s), %d detection(s)", len(_cache), len(_ocr_cache))


def save_cache() -> None:
    # Serialize under a lock and write atomically (temp file + replace) so a
    # crash or concurrent request mid-write can't corrupt the cache file.
    with _cache_save_lock:
        data = {
            "translate_cache": _cache,
            "ocr_cache": {url: result.model_dump() for url, result in _ocr_cache.items()},
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
        is_sfx=box.is_sfx,
    )


def build_payload(
    img: Image.Image, result: TranslationResult, engine: str, glossary: list[GlossaryEntry], settings: Settings
) -> dict:
    """Turns one image's raw Gemini detection into the final API response
    shape: clamps/cleans each box and, for non-Gemini engines, translates
    it via the selected engine (Gemini's own translation is already baked
    into `result` from detection time)."""
    boxes = []
    for raw_box in result.boxes:
        box = clamp_box(denormalize_box(raw_box, img.width, img.height), img.width, img.height)
        if box is None:
            continue

        english = box.english
        if engine == "argos":
            english = translate_with_glossary(box.korean, glossary, argos_translate_text)
        elif engine == "azure":
            english = azure_translate_text(box.korean, glossary, settings)
        elif engine == "papago":
            english = translate_with_glossary(box.korean, glossary, lambda t: papago_translate_text(t, settings))

        boxes.append(
            {
                "x": box.x,
                "y": box.y,
                "w": box.w,
                "h": box.h,
                "korean": box.korean,
                "english": clean_text(english),
                "color": sample_border_color(img, box),
                "sfx": box.is_sfx,
            }
        )

    return {"width": img.width, "height": img.height, "boxes": boxes}


@app.post("/translate")
def translate(req: TranslateRequest):
    if req.engine not in ("gemini", "argos", "papago", "azure"):
        raise HTTPException(status_code=400, detail=f"Unknown engine: {req.engine}")

    cache_key = f"{req.image_url}::{req.engine}::{glossary_cache_key(req.glossary)}"
    if not req.force and cache_key in _cache:
        return _cache[cache_key]

    settings = get_settings()
    image_bytes, img = fetch_image(req.image_url)

    if req.engine == "gemini":
        result = call_gemini(settings, image_bytes, req.glossary)
    else:
        result = get_ocr_boxes(settings, req.image_url, image_bytes, req.force)

    payload = build_payload(img, result, req.engine, req.glossary, settings)
    _cache[cache_key] = payload
    save_cache()
    return payload


@app.post("/translate_batch")
def translate_batch(req: TranslateBatchRequest):
    if req.engine not in ("gemini", "argos", "papago", "azure"):
        raise HTTPException(status_code=400, detail=f"Unknown engine: {req.engine}")
    if len(req.image_urls) > 25:
        raise HTTPException(status_code=400, detail="Batch too large (max 25 images)")

    settings = get_settings()

    # Anything already fully cached for this exact engine+glossary needs no
    # Gemini involvement at all — only genuinely new work goes into detection.
    cache_keys = [f"{url}::{req.engine}::{glossary_cache_key(req.glossary)}" for url in req.image_urls]
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

        if req.engine == "gemini":
            results = call_gemini_batch_chunked(settings, images_bytes, req.glossary)
        else:
            results = get_ocr_boxes_batch(settings, pending_urls, images_bytes, req.force)

        for idx, img, result in zip(pending_indices, imgs, results):
            payload = build_payload(img, result, req.engine, req.glossary, settings)
            payloads[idx] = payload
            _cache[cache_keys[idx]] = payload

        save_cache()

    return {"results": payloads}


@app.post("/retranslate")
def retranslate(req: RetranslateRequest):
    if req.engine not in ("gemini", "argos", "papago", "azure"):
        raise HTTPException(status_code=400, detail=f"Unknown engine: {req.engine}")

    settings = get_settings()

    if req.engine == "gemini":
        english = call_gemini_text(settings, req.korean, req.glossary)
    elif req.engine == "argos":
        english = translate_with_glossary(req.korean, req.glossary, argos_translate_text)
    elif req.engine == "azure":
        english = azure_translate_text(req.korean, req.glossary, settings)
    else:  # papago
        english = translate_with_glossary(
            req.korean, req.glossary, lambda t: papago_translate_text(t, settings)
        )

    return {"english": clean_text(english)}


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run("main:app", host=settings.host, port=settings.port)
