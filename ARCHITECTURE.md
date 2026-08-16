# Architecture

Technical reference for how this thing is built. For install/usage, see [README.md](README.md).

## Tech stack

**Extension** (`extension/`)
- Manifest V3 Chrome extension, vanilla JS/CSS — no build step, no framework.
- `content.js` — injected into `comic.naver.com/webtoon/detail*`. Finds panel images, builds the floating control panel, renders overlays, owns all UI state (glossary, detector/engine choice, appearance settings), and drives the boundary-stitch and manual-selection features.
- `background.js` — the extension's service worker. Its only job is proxying `fetch` calls to the local backend; content scripts can't reliably make cross-origin requests themselves without running into the page's CORS/CSP.
- `chrome.storage.local` — persists glossary, detector/engine choice, and appearance settings across sessions.

**Backend** (`backend/`)
- Python, FastAPI + Uvicorn, single file (`main.py`).
- [`paddleocr`](https://pypi.org/project/paddleocr/) + `paddlepaddle` — the default detector (PP-OCRv5, Korean model): free, local, no API key.
- [`google-genai`](https://pypi.org/project/google-genai/) — Gemini SDK, used as the alternate detector and as one of the translation engines.
- `Pillow` — downloads panel images server-side and samples pixel colors; also sidesteps the browser CORS restrictions Naver's CDN would otherwise impose on client-side image reads.
- Plain `requests` calls to Azure's and DeepL's REST translation APIs for two of the optional engines.
- `transformers` + `torch` (**not** installed by default — see `requirements.txt`) — only needed for the optional local `nllb` engine.

## Why a local backend instead of pure client-side

Two hard requirements ruled out doing everything in the content script:
1. Naver's CDN doesn't send CORS headers, so the browser can't read pixel data out of the images directly (needed for background-color sampling) or make certain cross-origin calls cleanly from a content script.
2. API keys (Gemini, Azure, DeepL) can't live in an extension's client-side code without being trivially extractable. They live in `backend/.env` on your machine and never touch the browser.

The backend also does all image fetching itself, which means it's the single point where the "only allow `*.pstatic.net`" host allowlist and the request-count throttling live — the extension never talks to Naver's CDN or Google's/Microsoft's/DeepL's translation APIs directly.

## API surface

Five endpoints, all in `main.py`:

- `GET /health` — liveness check the extension polls on load.
- `POST /translate` — one image, full detect+translate. Used internally by the batch path below, per image URL not already cached.
- `POST /translate_batch` — the extension's actual entry point for "Translate Episode": a list of image URLs, chunked client-side (see Batching), with per-image results returned together. Only images missing from cache actually do any work.
- `POST /translate_region` — manual selection: one or more server-side crops (possibly spanning an image boundary), composited into one image, detected/translated as a single unit. See Manual region selection below.
- `POST /retranslate` — takes just an already-known Korean string (no image, no re-detection) and returns a fresh English translation. Backs both the per-box ↻ button and the automatic boundary-stitch merge.

## Request flow (translating an episode)

```
content.js: find <img id="content_image_N"> elements
    → chunk into groups (BATCH_SIZE=5 for the gemini detector; 1 for
      paddleocr, which has no quota to conserve so per-image progress
      updates cost nothing)
    → for each chunk: background.js → POST /translate_batch
                                          ↓
                        backend: fetch each image (Pillow)
                                          ↓
              detector == paddleocr?  →  run locally per image, no
                                          quota, confidence-filtered,
                                          deduplicated
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
content.js: renderOverlay() per image — draws a label over each detected
            box (or an invisible hover-reveal hit area, if "Always show
            translation" is off), then checks the image's top/bottom-edge
            text against the previous image's for a boundary-stitch merge
```

## Detectors

Detection (finding text regions + reading the Korean) and translation (turning that Korean into English) are independent, selectable axes — see `build_payload()` and the `_detect()` dispatcher in `main.py`. Three "detect Korean text + give me a bounding box" alternatives to Gemini were evaluated for a free/local pipeline:

- **manga-image-translator** (open source, purpose-built for comic translation) — its own docs state Korean detection isn't well supported; it's tuned for Japanese manga. Not integrated.
- **A local LLM** (e.g. EXAONE via Ollama) — would likely work well, but needs the end user to install Ollama, download a multi-GB model, and own reasonably capable hardware — a bad fit for something meant to be usable by more than one technical person. Not integrated.
- **PaddleOCR** (PP-OCRv5, Korean model) — tested directly against real panels rather than assumed. Results: 94.6% confidence exact match on real printed dialogue, but garbage or missed detections on stylized SFX lettering (confidence typically under 70%). **This is what's actually integrated, and is the default detector.**

Because PaddleOCR's failure mode (low confidence on stylized/artistic text) correlates strongly with SFX specifically, `PADDLE_MIN_CONFIDENCE` (default 0.85) doubles as a de facto SFX filter — dropped results are simply not shown, rather than displaying a wrong translation. This isn't a real SFX classifier, just an empirically useful side effect.

That filter isn't complete on its own, though: stylized SFX lettering sometimes gets misread as simple Latin digits/letters ("00", "6", "OK", "A") *confidently* enough to survive the confidence threshold. `_has_hangul()` catches this second failure mode directly — real Korean dialogue always contains at least one Hangul syllable character (`가-힣`), so any detection with none is dropped regardless of its confidence score. Confirmed against two panels that previously produced exactly this garbage: both now correctly return zero boxes instead of a nonsense "translation."

For speed, `get_paddle_ocr()` uses PP-OCRv5's lighter "mobile" detection model (paired explicitly with the Korean "mobile" recognition model — leaving the recognition model implicit after changing the detector silently swaps in a non-Korean default) and disables three document-processing submodules (page-rotation classification, dewarping, text-line-orientation) meant for scanned/photographed input that a flat digital panel never needs. Measured: ~10x faster (28s → ~3s/image on CPU) with comparable-or-better accuracy — dropping the unneeded submodules actually *fixed* one recognition rather than just saving time, plausibly because dewarping an already-flat image was introducing distortion. The one honest cost: the faster model occasionally misreads a character while still reporting confidence above the filter threshold, so `PADDLE_MIN_CONFIDENCE` isn't a hard accuracy guarantee, just a strong correlation.

**Duplicate detections.** PaddleOCR's detector occasionally emits two overlapping boxes for the exact same physical text line — observed on panels with a busy screentone/hatching background, which appears to confuse its internal box suppression. Both get recognized as identical text and, left alone, both survive into two overlapping, doubly-rendered translations. `_dedupe_lines()` fixes this: when two detections share identical text and >40% IoU overlap, only the higher-confidence one is kept.

**Line grouping.** Gemini, as a full vision-language model, naturally treats wrapped multi-line dialogue within one speech bubble as a single sentence. PaddleOCR is a generic line-level text detector with no such concept — confirmed directly: a 3-line bubble came back as 3 separate detections, each translated in isolation into disconnected fragments ("low-tech" / "How on earth" / garbled leftovers) instead of one coherent sentence. `group_text_lines()` fixes this with a standard OCR post-processing heuristic: sort detections top-to-bottom, then greedily merge a line into the previous group when it has horizontal overlap with the group (same column of text) *and* a small vertical gap relative to line height (close enough to be the next line of the same paragraph rather than a different bubble entirely). Verified both directions — wrapped lines within one bubble merge into one sentence, and two genuinely separate bubbles with a large gap between them stay separate.

One Windows-specific gotcha baked into `get_paddle_ocr()`: PaddlePaddle 3.3.1's default oneDNN acceleration path throws a `NotImplementedError` on CPU inference on this platform — confirmed via testing, not hypothetical. `enable_mkldnn=False` works around it and is hardcoded, not left as a manual step.

Gemini remains available as a detector for full SFX coverage or when PaddleOCR's filtering drops text worth seeing. When Gemini is used for translation but *not* detection, its translation is no longer bundled with detection — each box gets a separate lightweight text-only Gemini call (`call_gemini_text`, the same function `/retranslate` uses) instead of relying on the detection call's own `english` field.

## Cross-image boundary stitching

Naver slices a tall episode into fixed-height image files with no regard for where a speech bubble happens to fall, so a bubble's text can be split across the boundary between two adjacent images — each image gets detected/translated independently, so a split bubble comes back as two disconnected fragments (confirmed directly: "저기술은 대체 어떻게" on one image, "작동하는 거지." on the next, translated as two unrelated sentences instead of one).

Since panels render in strict reading order, `maybeStitchWithPrevious()` in `content.js` catches this after the fact, right after each image's overlay renders: if the previous image's bottom-most text sat within the last 6% of its height (`EDGE_MARGIN`), and the current image's top-most text sits within the first 6% of *its* height, with substantial horizontal overlap between the two, they're almost certainly one sentence. The two Korean fragments are joined and sent through `/retranslate` as one string, both original fragment boxes are removed, and a single merged box is rendered spanning the union of their original page positions — not updated in place with the same text, which earlier left two differently-sized boxes both showing the identical full sentence (read as a duplicate/rendering bug rather than one merged bubble).

The merged box's page position is computed from `getBoundingClientRect()` *before* the `/retranslate` round-trip, the same defensive pattern manual selection uses below — so it lands exactly on the original two fragments' spot even if the page scrolls while the request is in flight.

**Known gap:** this only handles a bubble split *between* full lines (one complete line on each image). It doesn't handle Naver's slice cutting *through* a single line's character row — confirmed directly on a real panel: "생성했을 때가 가장" straddled the exact pixel boundary between two images, and both halves came back as garbled, sub-threshold reads (0.782 and 0.613 confidence) that got silently dropped by the confidence filter before the client ever saw them to stitch. Neither fragment is a valid detection to begin with, so there's nothing for the stitch logic to find and merge. Fixing this automatically would mean re-compositing the actual pixels at *every* image boundary (most of which are fine) and re-running OCR on the reconstructed strip just to catch a comparatively rare exact-pixel-cut case — deliberately not built, the added latency wasn't judged worth it as an always-on cost.

## Manual region selection

Instead, there's a targeted, user-triggered version of the same fix for that gap: drag a selection over any part of the page (`startSelectMode`/`onSelectMouseUp` in `content.js`); the client computes which panel image(s) the selection overlaps and the crop rectangle within each image's own natural pixel coordinates (working entirely in viewport/`getBoundingClientRect()` space, since client-side canvas cropping is blocked the same way pixel color-sampling would be — Naver's CDN sends no CORS headers). `POST /translate_region` fetches and crops server-side, composites multiple crops into one image when the selection spans a boundary (the exact scenario above — verified directly: feeding it crops straddling that same boundary correctly reconstructed "지면을 향해 생성했을 때가 가장 강력하지만" as one sentence), runs the selected detector+engine, and returns one merged Korean/English pair treated as a single unit.

Rendered via the shared `renderPageBox()` helper, positioned in page (document) coordinates rather than tied to any one image's coordinate system, since a selection may span more than one — the render rect is captured before the network round-trip so it doesn't drift if the page scrolls while waiting. The same helper backs the boundary-stitch merge above; both are "a box that isn't naturally owned by one source image."

## Coordinate system

Gemini is asked for bounding boxes as `[ymin, xmin, ymax, xmax]` normalized to 0–1000 (Google's own documented spatial-grounding convention), not raw pixel numbers — asking it to freehand pixel coordinates produced visibly worse box placement in testing. The backend denormalizes to actual pixels (`x = xmin/1000 * image_width`, etc.) before clamping to the image bounds and returning to the extension.

The extension in turn never uses pixel offsets either for per-image boxes — each overlay box is positioned as a **percentage** of its own wrapper `<div>`, which is sized to exactly match the `<img>` element via CSS (`width: fit-content`). This was a deliberate fix for a real bug: Naver's own CSS centers each ~690px panel inside a wider (`min-width: 960px`) container, and a naive `position: absolute` overlay sized to that wider container put boxes in the blank margin next to the art instead of on top of it. Manual-selection and stitched boxes are the exception — they're positioned in absolute page coordinates instead, since they aren't tied to a single image's wrapper (see above).

## Overlay rendering

Each detected region gets two DOM elements:
- A **hit area** (`.ct-bubble`) sized exactly to the detected box, with a faint dashed border — this is what receives hover/click events, and is also what stays visible on its own when hover-only mode (below) is active.
- A **label** (`.ct-bubble-label`) inside it, sized to its own text content (not stretched to fill the hit area), so short translations don't turn into oversized colored slabs inheriting the original (often much larger) Korean text's bounding box. `fitLabel()` additionally shrinks the label's font size (down to a floor, `MIN_FONT_SIZE`) until it actually fits within the box, since bubble sizes vary panel to panel and a single global font-size setting can't fit all of them — the "Text size" control is a ceiling, not a fixed value, and re-fits every rendered label live when changed.

**Visibility has two modes**, controlled by the "Always show translation" checkbox (`alwaysShowLabels`, on by default):
- **Always shown** (default): labels render directly on the page at all times.
- **Hover-only** (`.ct-labels-hover-only` class on `<html>`): labels stay invisible (only the dashed hit-area outline shows) until hovered or clicked-active — the original behavior, still available for readers who'd rather not have translated text permanently overlaid on the art.

Text size, text color, font, and the hover-only toggle are all CSS custom properties / classes set on `document.documentElement`, so the panel's controls can change every currently-rendered label live with no re-render needed.

## Per-box operations

Two independent ways to jump between a box on the page and its row in the Translations tab (`flash()`/`flashHighlight()`/`jumpToRow()` in `content.js`): clicking a row scrolls to and briefly highlights its box on the page; clicking a box switches to the Translations tab and scrolls to its row. Both directions share the same registry (`boxId → {boxEl, rowEl, labelEl, korean, english}`) as the single source of truth, so a right-click copy or a ↻ refresh always reflects the latest text rather than a stale value captured at render time.

The ↻ button on a Translations row re-translates *only that one bubble* — `POST /retranslate` with just its already-known Korean text, no image fetch, no re-detection, every other box on the panel untouched. Useful immediately after editing the glossary, without paying to re-detect (or re-run Gemini vision on) the whole panel.

## Caching

Three layers, all persisted to `backend/cache.json` (survives backend restarts) as well as held in memory:

- **`_ocr_cache`** (`image_url → raw Gemini detection`): Gemini-detected Korean text + box coordinates, independent of translation engine or glossary.
- **`_paddle_cache`** (`image_url → raw PaddleOCR detection`): same idea, for the PaddleOCR detector. Kept as a separate dict from `_ocr_cache` rather than a single detector-keyed cache — deliberately, so adding PaddleOCR didn't require touching the existing Gemini-detector cache/behavior at all.
- **`_cache`** (`image_url::detector::engine::glossary → final response`): the fully-translated, ready-to-render payload. Keying on the glossary's content means editing the glossary and clicking ↻ naturally busts the cache for affected panels without any manual invalidation step.

Detection is deliberately engine-agnostic (whichever detector ran, its result is reused across engine/glossary switches) — **except** the Gemini-detector + Gemini-engine combination, where translation is baked into the same call as detection (glossary terms enforced via prompt instructions, which only Gemini can act on). That's the one case where switching engine or glossary costs a fresh Gemini call; every other combination re-translates already-cached Korean text directly.

## Batching

The free Gemini tier's daily cap (discovered empirically: 500 requests/day for `gemini-3.5-flash-lite`) is on **request count**, not tokens or image size per request. This only matters when the Gemini detector is selected — PaddleOCR runs locally per image with no quota to conserve, so the extension chunks at size 1 for it (a progress update after every single panel) but at `BATCH_SIZE` (5, matching the backend's own `GEMINI_BATCH_SIZE` default) for the Gemini detector. For the Gemini detector, `GEMINI_BATCH_SIZE` controls how many panel images go into a single Gemini call — the prompt labels each image (`=== Image 3 ===`) and asks for a matching ordered list of per-image results, cutting Gemini calls per episode roughly 5x. The extension calls `/translate_batch` once per chunk (preserving incremental per-panel progress in the UI, tracked via a progress bar); the backend also enforces the batch size server-side regardless of what a client sends, so a misbehaving or future client can't accidentally request an unbounded single Gemini call.

Also worth knowing: the daily-quota error and a transient rate-limit error look identical at first glance (same `429 RESOURCE_EXHAUSTED`), but Google's `quotaId` distinguishes them (`"PerDay"` vs not). `_is_daily_quota_exhausted()` checks for that and fails immediately instead of retrying — retrying a daily cap with exponential backoff just burns several minutes to arrive at the same failure a daily cap can't recover from until it resets.

## Translation engines

Independent of the detector, the engine controls which service turns detected Korean text into English — see `build_payload()`, `translate_text_via_engine()`, and `_validate_engine_detector()` in `main.py`.

- **Gemini**: prompt instructions ("use exactly this English form whenever you see this Korean text") for glossary locking — most reliable, since it's an instruction-following model. Bundled with detection when Gemini is also the detector; otherwise a separate lightweight text-only call (`call_gemini_text`).
- **Azure**: its first-party `<mstrans:dictionary>` markup for glossary terms, officially documented as reliable for proper nouns — the best mechanism available among the non-Gemini engines. Plain `requests` call to `api.cognitive.microsofttranslator.com`.
- **DeepL**: plain `requests` call to DeepL's `/v2/translate`. Free-tier API keys are suffixed `:fx` and only work against `api-free.deepl.com`; paid keys use `api.deepl.com` — `deepl_translate_text()` picks the host by inspecting the key itself rather than a separate settings flag. Glossary terms locked via the placeholder-swap trick below (DeepL does support a first-party glossary API, but it requires pre-creating a persistent glossary resource via a separate endpoint — not worth the extra complexity here versus the swap trick's "works with zero setup" property).
- **NLLB**: a real pretrained multilingual model (`facebook/nllb-200-distilled-600M`) run locally via `transformers`, not a cloud call — no API key, no quota, no cost. Loaded lazily and cached as a module-level singleton (`get_nllb_model()`, same pattern as `get_paddle_ocr()`), guarded by a lock so concurrent requests don't trigger duplicate loads. Uses `AutoTokenizer`/`AutoModelForSeq2SeqLM` + `.generate()` directly rather than the `transformers.pipeline("translation", ...)` shortcut — confirmed the installed `transformers` version's pipeline task registry no longer recognizes the `"translation"` task string and raises `KeyError`, so the lower-level API is what's actually stable. `torch`/`transformers`/`sentencepiece` are deliberately **not** in `requirements.txt` (see Tech stack) — `get_nllb_model()` raises a clear `HTTPException` with the install command if they're missing, rather than crashing the whole backend for users who never select this engine.

**Glossary term-locking for non-Gemini/non-Azure engines** (DeepL, NLLB): neither takes instructions, so `translate_with_glossary()` swaps each glossary term for a placeholder token (`@0@`, `@1@`, ...) before translating, then swaps the placeholder back for the reader's fixed English form after. Best-effort rather than guaranteed — small MT models don't always preserve unfamiliar tokens perfectly — but it's the standard trick for term-locking a black-box translator.

## Security-relevant decisions

- `ALLOWED_IMAGE_HOST_SUFFIXES = (".pstatic.net",)` — the backend will only fetch images from Naver's CDN, so it can't be turned into an open image-fetching proxy by a malicious request.
- CORS on the backend is restricted to `chrome-extension://*` origins only.
- All translated/OCR'd text rendered into the page DOM goes through `textContent`, never `innerHTML`, since it ultimately originates from third-party image content the extension doesn't control.
- API keys live only in `backend/.env` (gitignored), read server-side; the browser extension never sees them. `backend/.env.example` (the checked-in template, containing no real secrets) is intentionally tracked in git — only `.env` itself is ignored.
