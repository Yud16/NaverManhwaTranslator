# Architecture

Technical reference for how this thing is built. For install/usage, see [README.md](README.md).

## Tech stack

**Extension** (`extension/`)
- Manifest V3 Chrome extension, vanilla JS/CSS — no build step, no framework.
- `content.js` — injected into `comic.naver.com/webtoon/detail*`. Finds panel images, builds the floating control panel, renders overlays, owns all UI state (glossary, engine choice, appearance settings).
- `background.js` — the extension's service worker. Its only job is proxying `fetch` calls to the local backend; content scripts can't reliably make cross-origin requests themselves without running into the page's CORS/CSP.
- `chrome.storage.local` — persists glossary, engine choice, and appearance settings across sessions.

**Backend** (`backend/`)
- Python, FastAPI + Uvicorn, single file (`main.py`).
- [`paddleocr`](https://pypi.org/project/paddleocr/) + `paddlepaddle` — the default detector (PP-OCRv5, Korean model): free, local, no API key.
- [`google-genai`](https://pypi.org/project/google-genai/) — Gemini SDK, used as the alternate detector and as one of the translation engines.
- `Pillow` — downloads panel images server-side and samples pixel colors; also sidesteps the browser CORS restrictions Naver's CDN would otherwise impose on client-side image reads.
- `argostranslate` — optional fully-local translation engine (no API key, no network call).
- Plain `requests` calls to Papago's and Azure's REST translation APIs for the other two optional engines.

## Why a local backend instead of pure client-side

Two hard requirements ruled out doing everything in the content script:
1. Naver's CDN doesn't send CORS headers, so the browser can't read pixel data out of the images directly (needed for background-color sampling) or make certain cross-origin calls cleanly from a content script.
2. API keys (Gemini, Azure, Papago) can't live in an extension's client-side code without being trivially extractable. They live in `backend/.env` on your machine and never touch the browser.

The backend also does all image fetching itself, which means it's the single point where the "only allow `*.pstatic.net`" host allowlist and the request-count throttling live — the extension never talks to Naver's CDN or Google's/Microsoft's/Naver's translation APIs directly.

## Request flow (translating an episode)

```
content.js: find <img id="content_image_N"> elements
    → chunk into groups of BATCH_SIZE (default 5)
    → for each chunk: background.js → POST /translate_batch
                                          ↓
                        backend: fetch each image (Pillow)
                                          ↓
              detector == paddleocr?  →  run locally per image, no
                                          quota, confidence-filtered
              detector == gemini?    →  one Gemini call covers the
                                          whole chunk (detection, and
                                          translation too if engine
                                          is *also* gemini)
                                          ↓
              engine != gemini, or engine == gemini with a non-gemini
              detector?  →  one translation-API call per text region
                                          ↓
                        clamp/clean boxes, sample bg color,
                        cache result, return to extension
                                          ↓
content.js: renderOverlay() per image — draws a transparent hit-area
            per box (dashed outline) with a hover-reveal label inside
```

Individual per-line re-translation (the ↻ button on a Translations-tab row, used after editing the glossary) is a separate lightweight path: `POST /retranslate` takes just the already-known Korean text and returns a new English string — no image, no re-detection.

## Detectors

Detection (finding text regions + reading the Korean) and translation (turning that Korean into English) are independent, selectable axes — see `build_payload()` and the `_detect()` dispatcher in `main.py`. Three "detect Korean text + give me a bounding box" alternatives to Gemini were evaluated for a free/local pipeline:

- **manga-image-translator** (open source, purpose-built for comic translation) — its own docs state Korean detection isn't well supported; it's tuned for Japanese manga. Not integrated.
- **A local LLM** (e.g. EXAONE via Ollama) — would likely work well, but needs the end user to install Ollama, download a multi-GB model, and own reasonably capable hardware — a bad fit for something meant to be usable by more than one technical person. Not integrated.
- **PaddleOCR** (PP-OCRv5, Korean model) — tested directly against real panels rather than assumed. Results: 94.6% confidence exact match on real printed dialogue, but garbage or missed detections on stylized SFX lettering (confidence typically under 70%). **This is what's actually integrated, and is the default detector.**

Because PaddleOCR's failure mode (low confidence on stylized/artistic text) correlates strongly with SFX specifically, `PADDLE_MIN_CONFIDENCE` (default 0.85) doubles as a de facto SFX filter — dropped results are simply not shown, rather than displaying a wrong translation. This isn't a real SFX classifier (unlike Gemini's own `is_sfx` field), just an empirically useful side effect.

That filter isn't complete on its own, though: stylized SFX lettering sometimes gets misread as simple Latin digits/letters ("00", "6", "OK", "A") *confidently* enough to survive the confidence threshold. `_has_hangul()` catches this second failure mode directly — real Korean dialogue always contains at least one Hangul syllable character (`가-힣`), so any detection with none is dropped regardless of its confidence score. Confirmed against two panels that previously produced exactly this garbage: both now correctly return zero boxes instead of a nonsense "translation."

For speed, `get_paddle_ocr()` uses PP-OCRv5's lighter "mobile" detection model (paired explicitly with the Korean "mobile" recognition model — leaving the recognition model implicit after changing the detector silently swaps in a non-Korean default) and disables three document-processing submodules (page-rotation classification, dewarping, text-line-orientation) meant for scanned/photographed input that a flat digital panel never needs. Measured: ~10x faster (28s → ~3s/image on CPU) with comparable-or-better accuracy — dropping the unneeded submodules actually *fixed* one recognition rather than just saving time, plausibly because dewarping a already-flat image was introducing distortion. The one honest cost: the faster model occasionally misreads a character while still reporting confidence above the filter threshold, so `PADDLE_MIN_CONFIDENCE` isn't a hard accuracy guarantee, just a strong correlation.

**Line grouping.** Gemini, as a full vision-language model, naturally treats wrapped multi-line dialogue within one speech bubble as a single sentence. PaddleOCR is a generic line-level text detector with no such concept — confirmed directly: a 3-line bubble came back as 3 separate detections, each translated in isolation into disconnected fragments ("low-tech" / "How on earth" / garbled leftovers) instead of one coherent sentence. `group_text_lines()` fixes this with a standard OCR post-processing heuristic: sort detections top-to-bottom, then greedily merge a line into the previous group when it has horizontal overlap with the group (same column of text) *and* a small vertical gap relative to line height (close enough to be the next line of the same paragraph rather than a different bubble entirely). Verified both directions — wrapped lines within one bubble merge into one sentence, and two genuinely separate bubbles with a large gap between them stay separate.

One Windows-specific gotcha baked into `get_paddle_ocr()`: PaddlePaddle 3.3.1's default oneDNN acceleration path throws a `NotImplementedError` on CPU inference on this platform — confirmed via testing, not hypothetical. `enable_mkldnn=False` works around it and is hardcoded, not left as a manual step.

Gemini remains available as a detector for full SFX coverage or when PaddleOCR's filtering drops text worth seeing. When Gemini is used for translation but *not* detection, its translation is no longer bundled with detection — each box gets a separate lightweight text-only Gemini call (`call_gemini_text`, the same function `/retranslate` uses) instead of relying on the detection call's own `english` field.

**Known gap: a line cut mid-row by an image boundary.** The client-side boundary stitch above handles a bubble split *between* lines (one full line on each image). It doesn't handle Naver's slice cutting *through* a single line's character row — confirmed directly on a real panel: "생성했을 때가 가장" straddled the exact pixel boundary between two images, and both halves came back as garbled, sub-threshold reads (0.782 and 0.613 confidence) that got silently dropped by the confidence filter before the client ever saw them to stitch. Fixing this automatically would mean re-compositing the actual pixels at *every* image boundary (most of which are fine) and re-running OCR on the reconstructed strip just to catch a comparatively rare exact-pixel-cut case — deliberately not built, the added latency wasn't judged worth it as an always-on cost.

Instead there's a targeted, user-triggered version of the same fix: **manual region selection** (`translateSelection`/`renderManualBox` in `content.js`, `POST /translate_region` in the backend). Drag a selection over any gap in the page; the client computes which panel image(s) the selection overlaps and the crop rectangle within each image's own natural pixel coordinates (working entirely in viewport/`getBoundingClientRect()` space, since client-side canvas cropping is blocked the same way pixel color-sampling would be — Naver's CDN sends no CORS headers). The backend fetches and crops server-side, composites multiple crops into one image when the selection spans a boundary (the exact scenario above — verified directly: feeding it crops straddling that same boundary correctly reconstructed "지면을 향해 생성했을 때가 가장 강력하지만" as one sentence), runs the selected detector+engine, and returns one merged Korean/English pair treated as a single unit. Rendered as a floating overlay positioned at the original selection rect (captured before the network round-trip, so it doesn't drift if the page scrolls while waiting) rather than tied to any one image's coordinate system, since a selection may span more than one.

## Coordinate system

Gemini is asked for bounding boxes as `[ymin, xmin, ymax, xmax]` normalized to 0–1000 (Google's own documented spatial-grounding convention), not raw pixel numbers — asking it to freehand pixel coordinates produced visibly worse box placement in testing. The backend denormalizes to actual pixels (`x = xmin/1000 * image_width`, etc.) before clamping to the image bounds and returning to the extension.

The extension in turn never uses pixel offsets either — each overlay box is positioned as a **percentage** of its own wrapper `<div>`, which is sized to exactly match the `<img>` element via CSS (`width: fit-content`). This was a deliberate fix for a real bug: Naver's own CSS centers each ~690px panel inside a wider (`min-width: 960px`) container, and a naive `position: absolute` overlay sized to that wider container put boxes in the blank margin next to the art instead of on top of it.

## Overlay rendering (hover-reveal, not always-on)

Each detected region gets two DOM elements:
- A **hit area** (`.ct-bubble`) sized exactly to Gemini's detected box, with just a faint dashed border — this is what you hover to trigger the reveal, and it's deliberately kept at the *original* (sometimes oversized) detection box so the trigger zone is easy to find.
- A **label** (`.ct-bubble-label`) inside it, sized to its own text content (not stretched to fill the hit area), visible only on `:hover`/`.ct-active`. This avoids two failure modes from earlier iterations: permanently-visible boxes blocking the art, and short translations turning into oversized colored slabs because they inherited the original (often much larger) Korean text's bounding box.

Text size, background opacity, and text color are all CSS custom properties (`--ct-label-font-size` etc.) set on `document.documentElement`, so the panel's controls can change every currently-rendered label live with no re-render needed.

## Caching

Three layers, all persisted to `backend/cache.json` (survives backend restarts) as well as held in memory:

- **`_ocr_cache`** (`image_url → raw Gemini detection`): Gemini-detected Korean text + box coordinates, independent of translation engine or glossary.
- **`_paddle_cache`** (`image_url → raw PaddleOCR detection`): same idea, for the PaddleOCR detector. Kept as a separate dict from `_ocr_cache` rather than a single detector-keyed cache — deliberately, so adding PaddleOCR didn't require touching the existing Gemini-detector cache/behavior at all.
- **`_cache`** (`image_url::detector::engine::glossary → final response`): the fully-translated, ready-to-render payload. Keying on the glossary's content means editing the glossary and clicking ↻ naturally busts the cache for affected panels without any manual invalidation step.

Detection is deliberately engine-agnostic (whichever detector ran, its result is reused across engine/glossary switches) — **except** the Gemini-detector + Gemini-engine combination, where translation is baked into the same call as detection (glossary terms enforced via prompt instructions, which only Gemini can act on). That's the one case where switching engine or glossary costs a fresh Gemini call; every other combination re-translates already-cached Korean text directly.

## Batching

The free Gemini tier's daily cap (discovered empirically: 500 requests/day for `gemini-3.5-flash-lite`) is on **request count**, not tokens or image size per request. This only matters when the Gemini detector is selected — PaddleOCR runs locally per image with no quota to conserve, so `/translate_batch` just loops over it directly. For the Gemini detector, `GEMINI_BATCH_SIZE` (default 5) controls how many panel images go into a single Gemini call — the prompt labels each image (`=== Image 3 ===`) and asks for a matching ordered list of per-image results, cutting Gemini calls per episode roughly 5x. The extension chunks an episode's images into groups of this size and calls `/translate_batch` once per chunk (preserving incremental per-panel progress in the UI); the backend also enforces the batch size server-side regardless of what a client sends, so a misbehaving or future client can't accidentally request an unbounded single Gemini call.

Also worth knowing: the daily-quota error and a transient rate-limit error look identical at first glance (same `429 RESOURCE_EXHAUSTED`), but Google's `quotaId` distinguishes them (`"PerDay"` vs not). `_is_daily_quota_exhausted()` checks for that and fails immediately instead of retrying — retrying a daily cap with exponential backoff just burns several minutes to arrive at the same failure a daily cap can't recover from until it resets.

## Glossary term-locking

Each translation engine gets fixed Korean→English terms (character names, recurring phrases) enforced differently depending on what the engine actually supports:
- **Gemini**: prompt instructions ("use exactly this English form whenever you see this Korean text") — most reliable, since it's an instruction-following model.
- **Azure**: its first-party `<mstrans:dictionary>` markup, officially documented as reliable for proper nouns — the best mechanism available among the non-Gemini engines.
- **Papago / Argos**: neither takes instructions, so terms are locked via a placeholder swap (replace the Korean term with a token like `@0@` before translating, swap it back after) — the standard trick for term-locking a black-box translator, best-effort rather than guaranteed.

## Security-relevant decisions

- `ALLOWED_IMAGE_HOST_SUFFIXES = (".pstatic.net",)` — the backend will only fetch images from Naver's CDN, so it can't be turned into an open image-fetching proxy by a malicious request.
- CORS on the backend is restricted to `chrome-extension://*` origins only.
- All translated/OCR'd text rendered into the page DOM goes through `textContent`, never `innerHTML`, since it ultimately originates from third-party image content the extension doesn't control.
- API keys live only in `backend/.env` (gitignored), read server-side; the browser extension never sees them.
