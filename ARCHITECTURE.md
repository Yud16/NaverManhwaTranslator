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
- [`google-genai`](https://pypi.org/project/google-genai/) — Gemini SDK, used for text/bubble detection and (optionally) translation.
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
                    engine == gemini?  →  one Gemini call covers
                                           the whole chunk (detection
                                           + translation together)
                    engine != gemini?  →  one Gemini call for detection
                                           only, then N translation-API
                                           calls (one per text region)
                                          ↓
                        clamp/clean boxes, sample bg color,
                        cache result, return to extension
                                          ↓
content.js: renderOverlay() per image — draws a transparent hit-area
            per box (dashed outline) with a hover-reveal label inside
```

Individual per-line re-translation (the ↻ button on a Translations-tab row, used after editing the glossary) is a separate lightweight path: `POST /retranslate` takes just the already-known Korean text and returns a new English string — no image, no re-detection.

## Why detection always uses Gemini

Three "detect Korean text + give me a bounding box" alternatives were evaluated for a fully Gemini-free pipeline:
- **manga-image-translator** (open source, purpose-built for comic translation) — its own docs state Korean detection isn't well supported; it's tuned for Japanese manga.
- **PaddleOCR** — solid Korean OCR, but is a generic document/scene-text detector, not tuned for comic bubbles or stylized SFX lettering; never actually integrated.
- A local LLM (e.g. EXAONE via Ollama) — would work, but needs the end user to install Ollama, download a multi-GB model, and own reasonably capable hardware — a bad fit for something meant to be usable by more than one technical person.

So detection stays on Gemini's vision model across all engine modes. What's actually swappable is *translation*: once Gemini has found a region and read the Korean text, turning that into English can be done by Gemini itself, or handed to Argos/Papago/Azure instead.

## Coordinate system

Gemini is asked for bounding boxes as `[ymin, xmin, ymax, xmax]` normalized to 0–1000 (Google's own documented spatial-grounding convention), not raw pixel numbers — asking it to freehand pixel coordinates produced visibly worse box placement in testing. The backend denormalizes to actual pixels (`x = xmin/1000 * image_width`, etc.) before clamping to the image bounds and returning to the extension.

The extension in turn never uses pixel offsets either — each overlay box is positioned as a **percentage** of its own wrapper `<div>`, which is sized to exactly match the `<img>` element via CSS (`width: fit-content`). This was a deliberate fix for a real bug: Naver's own CSS centers each ~690px panel inside a wider (`min-width: 960px`) container, and a naive `position: absolute` overlay sized to that wider container put boxes in the blank margin next to the art instead of on top of it.

## Overlay rendering (hover-reveal, not always-on)

Each detected region gets two DOM elements:
- A **hit area** (`.ct-bubble`) sized exactly to Gemini's detected box, with just a faint dashed border — this is what you hover to trigger the reveal, and it's deliberately kept at the *original* (sometimes oversized) detection box so the trigger zone is easy to find.
- A **label** (`.ct-bubble-label`) inside it, sized to its own text content (not stretched to fill the hit area), visible only on `:hover`/`.ct-active`. This avoids two failure modes from earlier iterations: permanently-visible boxes blocking the art, and short translations turning into oversized colored slabs because they inherited the original (often much larger) Korean text's bounding box.

Text size, background opacity, and text color are all CSS custom properties (`--ct-label-font-size` etc.) set on `document.documentElement`, so the panel's controls can change every currently-rendered label live with no re-render needed.

## Caching

Two layers, both persisted to `backend/cache.json` (survives backend restarts) as well as held in memory:

- **`_ocr_cache`** (`image_url → raw Gemini detection`): the Korean text and box coordinates for an image, independent of which translation engine or glossary is active. Detection is the expensive/quota-limited part, so this is deliberately engine-agnostic — switching from Argos to Papago on an already-translated episode costs zero Gemini calls.
- **`_cache`** (`image_url::engine::glossary → final response`): the fully-translated, ready-to-render payload. Keying on the glossary's content means editing the glossary and clicking ↻ naturally busts the cache for affected panels without any manual invalidation step.

`gemini` as a translation engine is the one exception to the "engine-agnostic detection" rule: its translation is baked into the same call as detection (glossary terms are enforced via prompt instructions, which only Gemini can act on), so switching *to* Gemini after using another engine still costs a fresh Gemini call.

## Batching

The free Gemini tier's daily cap (discovered empirically: 500 requests/day for `gemini-3.5-flash-lite`) is on **request count**, not tokens or image size per request. `GEMINI_BATCH_SIZE` (default 5) controls how many panel images go into a single Gemini call — the prompt labels each image (`=== Image 3 ===`) and asks for a matching ordered list of per-image results, cutting Gemini calls per episode roughly 5x. The extension chunks an episode's images into groups of this size and calls `/translate_batch` once per chunk (preserving incremental per-panel progress in the UI); the backend also enforces the batch size server-side regardless of what a client sends, so a misbehaving or future client can't accidentally request an unbounded single Gemini call.

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
