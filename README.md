# Naver Webtoon Translator

Translates Korean speech bubbles/SFX on `comic.naver.com` episode pages,
overlaid directly on the page as you read. Two parts:

- `backend/` — a local FastAPI server that fetches a panel image, detects
  text regions via Gemini (your own API key), and translates each line
  using whichever engine you've selected (Gemini, Papago, or Argos).
- `extension/` — a Chrome extension (Manifest V3) that finds panel images on
  the page and draws hover-reveal translated labels over them.

Nothing is uploaded anywhere except the image bytes sent to Google's Gemini
API for detection, and (if selected) the extracted Korean text sent to
Naver's Papago API for translation. The backend only runs on your machine.

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

`GEMINI_MODEL` matters — Google's model names get deprecated/replaced over
time. `gemini-flash-lite-latest` is an alias Google keeps pointed at a
current model, so it shouldn't go stale the way a pinned model name will.
If translate calls start 404ing, that's the first thing to check.

Run it:

```bash
.venv/Scripts/python.exe main.py
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
4. It processes panels one at a time (to respect Gemini's free-tier rate
   limit) and adds hover-reveal labels as each one finishes — hover a
   dashed outline on the page, or a row in the **Translations** tab, to see
   the English text; either one highlights the other.
5. Click **Hide Translation** / **Show Translation** to toggle the overlay
   without re-translating.

### Panel controls

- **Text size / Bg opacity / Text color** — appearance of the revealed
  label, live-adjustable, persisted across sessions.
- **Engine** — which translator to use (see below).
- **Log / Translations / Glossary tabs**:
  - **Translations** lists every line found so far. Click a row to jump to
    its bubble on the page; right-click for **Copy Korean** / **Copy
    English** (handy for populating the glossary); the ↻ button
    re-translates just that panel.
  - **Glossary** — fixed Korean → English pairs (character names, recurring
    terms) applied to every future translation. Editing it doesn't require
    restarting anything — click ↻ on an already-translated panel afterward
    to re-apply.

### Translation engines

| Engine | Quality | Setup | Notes |
|---|---|---|---|
| **Gemini** (default) | Best in testing | Already set up | Glossary terms are enforced via prompt instructions — most reliable. Detection (finding text + reading Korean) always uses Gemini regardless of this setting. |
| **Azure** | Real MT quality, untested for this specific project's Korean dialogue but should clear Argos easily | Needs `AZURE_TRANSLATOR_KEY` (`AZURE_TRANSLATOR_REGION` too, unless the resource is "Global") in `backend/.env` — create a free Translator resource at [portal.azure.com](https://portal.azure.com) (F0 tier, 2M free characters/month, card-based signup — no Korean-phone verification wall) | Glossary terms use Azure's own first-party "dynamic dictionary" feature (officially documented as reliable for proper nouns), not a placeholder hack. |
| **Papago** | Korean-specialized, likely close to Gemini | Needs `NAVER_CLIENT_ID` / `NAVER_CLIENT_SECRET` in `backend/.env` — register a free app at [Naver Cloud Platform](https://console.ncloud.com) under AI/ML → Papago Translation (150,000 free characters/month) | Naver's signup phone-verifies against Korean carriers and can be a real dead end for non-Korean numbers. Glossary terms are locked in via a placeholder-swap trick. |
| **Argos** | Noticeably worse — tested it directly and it flat-out mistranslated a line ("기다렸다" → "Thank you") | None — installs its Korean↔English model automatically on first use, fully offline after that | Free with zero setup and no rate limit, useful as a fallback once you've burned through Gemini's daily quota, not recommended as a default. |

Switching engines (or editing the glossary) after a panel's already been
translated doesn't re-run Gemini's detection step — only Gemini's own
translation path needs a fresh Gemini call, since it bakes glossary
awareness into the prompt. Azure/Papago/Argos re-translate the already-detected
Korean text directly, so flipping between them is instant and free.

## How it works

Naver serves each episode as a stack of `<img>` tags with no selectable
text, so ordinary page translators (Google Translate, etc.) can't touch it.
This tool instead:

1. `content.js` finds the panel `<img>` elements
   (`#sectionContWide img[id^="content_image_"]`) and sends each image URL
   to `background.js`.
2. `background.js` (the extension's privileged background worker) calls the
   local backend — routing through the background script avoids the page's
   CORS/CSP restrictions that would block a direct fetch from the content
   script.
3. The backend downloads the image server-side (no browser CORS issue at
   all there) and sends it to Gemini, asking for every text region's
   bounding box (in Gemini's own normalized 0–1000 spatial-grounding
   format — noticeably more accurate than asking it to freehand pixel
   coordinates) plus the original Korean text.
4. Depending on the selected engine, that Korean text is translated by
   Gemini itself, by Papago's API, or by a local Argos Translate model.
5. The backend samples each region's border pixels with Pillow to
   approximate the bubble's background color (used for reference; the
   on-page label defaults to solid white, adjustable).
6. `content.js` wraps each image, and for each box draws an invisible
   (dashed-outline) hit area sized to the detected region, containing a
   label sized to its own text that only becomes visible on hover — no
   pixel manipulation of the original image needed.

## Known limitations

- Text drawn directly into the art (no clean bubble behind it) will show a
  visible colored patch rather than a seamless erase — real inpainting would
  need a separate image model, not implemented here.
- Gemini's free tier has both a per-minute rate limit and a small daily
  quota depending on model; translation is done one panel at a time with
  retry/backoff to stay under the per-minute limit, but a fully exhausted
  daily quota just fails until it resets — that's when the Argos fallback
  is useful.
- Box detection has some run-to-run variance (a re-translate of the exact
  same panel can find a slightly different number of boxes) since it comes
  from a vision model's best guess, not a deterministic detector.
- Only tested against `comic.naver.com/webtoon/detail` episode pages, not
  the app or other Naver comic surfaces.
