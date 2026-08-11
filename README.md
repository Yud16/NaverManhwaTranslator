# Naver Webtoon Translator

Translates Korean speech bubbles/SFX on `comic.naver.com` episode pages,
overlaid directly on the page as you read. Two parts:

- `backend/` — a local FastAPI server that fetches panel images, detects text
  regions using whichever detector you've selected (PaddleOCR by default, or
  Gemini), and translates each line using whichever engine you've selected
  (Gemini, Azure, DeepL, or NLLB).
- `extension/` — a Chrome extension (Manifest V3) that finds panel images on
  the page and draws translated labels directly over them.

With the default settings (PaddleOCR detector), nothing leaves your machine
except whatever the selected translation engine needs — Gemini's API only
gets involved if you switch the Detector or Engine to Gemini. The backend
only runs on your machine; no data is sent anywhere else.

For the technical breakdown (stack, request flow, caching, batching, why
things are built the way they are), see [ARCHITECTURE.md](ARCHITECTURE.md).

## 1. Backend setup

```bash
cd backend
py -3 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

Copy `.env.example` to `.env` and put your Gemini API key in it:

```
GOOGLE_API_KEY=your_actual_key
GEMINI_MODEL=gemini-flash-lite-latest
```

```bash
python main.py
```

You should see `Uvicorn running on http://127.0.0.1:8000`. Leave this
running in a terminal while you read webtoons. Check it's alive any time
with:

```bash
curl http://127.0.0.1:8000/health
```

## 2. Load the extension in Chrome

1. Open `chrome://extensions/`
2. Enable **Developer mode** (top-right toggle)
3. Click **Load unpacked**
4. Select the `extension/` folder

## 3. Use it

1. Make sure the backend is running.
2. Open any episode on `comic.naver.com/webtoon/detail?...`
3. A panel appears top-right. Click **Translate Episode**.
4. It processes panels in batches (chunked to respect Gemini's free-tier rate
   limit when the Gemini detector is selected) and shows a progress bar as
   each batch finishes. Translated labels render directly over each detected
   region as soon as they're ready.
5. Click **Hide Translation** / **Show Translation** to toggle the overlay
   without re-translating.
6. Missed a line — e.g. one split across the boundary between two Naver image
   slices, worse than the automatic boundary-stitch below can catch? Click
   **Select Area to Translate**, drag a box over it on the page, and it's
   translated as its own floating overlay (works even across a boundary
   between two adjacent images, via server-side compositing).

### Panel controls

- **Text size / Bg opacity / Text color** — appearance of the on-page
  labels, live-adjustable, persisted across sessions.
- **Always show translation** — on by default: labels render directly on the
  page. Turn it off to switch to invisible-until-hover instead (just a faint
  dashed outline at rest, full label on hover/click).
- **Show sound effects** — off by default. SFX boxes are still detected and
  translated either way; this only controls whether they're shown, and
  toggling it is instant (no re-translate).
- **Detector** — which method finds text regions + reads the Korean (see
  below).
- **Engine** — which translator turns that Korean into English (see below).
- **Log / Translations / Glossary tabs**:
  - **Translations** lists every line found so far. Click a row to jump to
    its box on the page (and vice versa — click a box on the page to jump to
    its row); right-click a row for **Copy Korean** / **Copy English**
    (handy for populating the glossary); the ↻ button re-translates just
    that one bubble, leaving everything else on the panel untouched.
  - **Glossary** — fixed Korean → English pairs (character names, recurring
    terms) applied to every future translation. Editing it doesn't require
    restarting anything — click ↻ on an already-translated line afterward to
    re-apply.

### Detectors

| Detector | Quality | Setup | Notes |
|---|---|---|---|
| **PaddleOCR** (default) | Excellent on real dialogue (94%+ confidence, exact matches in testing) — but blind to stylized SFX lettering, which it either misses or garbles | Installed automatically with the other backend dependencies (no API key). First use per process loads its models, taking a few seconds. | Fully free, fully local, no daily quota. Low-confidence reads are dropped rather than shown wrong (see `PADDLE_MIN_CONFIDENCE` in `.env`), which in practice filters out most SFX along with genuine misreads. Occasional duplicate/overlapping detections of the same line (seen on busy screentone backgrounds) are deduplicated automatically. |
| **Gemini** | Best overall — the only detector with real SFX coverage | Already set up | Subject to Gemini's free-tier daily quota. Switch to this when PaddleOCR's confidence filter is dropping text you want to see, or for panels that are mostly SFX. |

Switching detectors on an already-translated episode does trigger fresh
detection (there's no shortcut — the two methods find different things), but
switching *engines* afterward doesn't need to re-detect anything, regardless
of which detector found the text.

### Translation engines

| Engine | Quality | Setup | Notes |
|---|---|---|---|
| **Gemini** (default) | Best in testing | Already set up | When paired with the Gemini detector, translation is glossary-aware via prompt instructions in the same call as detection — most reliable. Paired with any other detector, it's just another per-line translator (via a lightweight text-only call), same as the engines below. |
| **Azure** | Real MT quality | Needs `AZURE_TRANSLATOR_KEY` (`AZURE_TRANSLATOR_REGION` too, unless the resource is "Global") in `backend/.env` — create a free Translator resource at [portal.azure.com](https://portal.azure.com) (F0 tier, 2M free characters/month, card-based signup — no Korean-phone verification wall) | Glossary terms use Azure's own first-party "dynamic dictionary" feature (officially documented as reliable for proper nouns), not a placeholder hack. |
| **DeepL** | Strong general MT quality | Needs `DEEPL_API_KEY` in `backend/.env` — get one at [deepl.com/your-account/keys](https://www.deepl.com/your-account/keys) (free tier: 500,000 characters/month) | Free-tier keys end in `:fx` and are auto-detected to hit the correct free-vs-paid API host. Glossary terms are locked in via a placeholder-swap trick. |
| **NLLB** | Usable, noticeably behind Gemini — a real pretrained multilingual model (Meta's `nllb-200-distilled-600M`), not a from-scratch project | None — needs `pip install torch transformers sentencepiece` (not installed by default; see `requirements.txt`) plus a one-time ~2.4GB model download from Hugging Face on first use, then fully offline | Free with zero API key and no rate limit — the fallback once you've burned through Gemini's daily quota or don't want to touch cloud APIs at all. Glossary terms are locked in via a placeholder-swap trick. |

Switching engines (or editing the glossary) after a panel's already been
translated doesn't re-run detection — only the Gemini-detector + Gemini-engine
combination needs a fresh Gemini call on an engine/glossary change, since
that's the one case where translation is baked into the detection call
itself. Every other engine re-translates the already-detected Korean text
directly, so flipping between them (or switching detectors, once that
detector's own detection is cached) is instant and free.

## How it works

Naver serves each episode as a stack of `<img>` tags with no selectable
text, so ordinary page translators (Google Translate, etc.) can't touch it.
This tool instead:

1. `content.js` finds the panel `<img>` elements
   (`#sectionContWide img[id^="content_image_"]`), chunks them into batches,
   and sends each batch's image URLs to `background.js`.
2. `background.js` (the extension's privileged background worker) calls the
   local backend — routing through the background script avoids the page's
   CORS/CSP restrictions that would block a direct fetch from the content
   script.
3. The backend downloads each image server-side (no browser CORS issue at
   all there) and runs the selected **detector** on it: PaddleOCR (local,
   confidence-filtered, deduplicated) or Gemini (bounding boxes in its own
   normalized 0–1000 spatial-grounding format — noticeably more accurate
   than asking it to freehand pixel coordinates), either way producing a
   list of text regions + the original Korean.
4. That Korean text is translated by the selected **engine** — Gemini
   itself, Azure, DeepL, or a local NLLB model.
5. The backend samples each region's border pixels with Pillow to
   approximate the bubble's background color (used for reference; the
   on-page label defaults to solid white, adjustable).
6. `content.js` wraps each image and, for each box, draws a label sized to
   its own text directly over the detected region (or an invisible
   dashed-outline hit area if "Always show translation" is off) — no pixel
   manipulation of the original image needed.
7. As each image finishes, the client checks whether its topmost/bottommost
   text sits right at that image's edge and lines up with the previous/next
   image's edge text — if so, it's almost certainly one sentence Naver's
   slicing split in two, and the two fragments get merged into a single
   stitched box automatically.

## Known limitations

- Text drawn directly into the art (no clean bubble behind it) will show a
  visible colored patch rather than a seamless erase — real inpainting would
  need a separate image model, not implemented here.
- Gemini's free tier has both a per-minute rate limit and a small daily
  quota depending on model; translation is done in batches with
  retry/backoff to stay under the per-minute limit, but a fully exhausted
  daily quota just fails until it resets — that's when switching to a
  non-Gemini engine (or the PaddleOCR detector, which needs no Gemini calls
  at all) is useful.
- Box detection has some run-to-run variance (a re-translate of the exact
  same panel can find a slightly different number of boxes) since it comes
  from a vision model's best guess, not a deterministic detector.
- The automatic boundary stitch only catches a dialogue line split *between*
  two full lines across an image boundary — it can't catch Naver's slice
  cutting *through* the middle of a single line's character row, since
  neither resulting fragment is a valid detection to begin with. Use
  **Select Area to Translate** for that case.
- Only tested against `comic.naver.com/webtoon/detail` episode pages, not
  the app or other Naver comic surfaces.
