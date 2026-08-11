(function () {
  const VIEWER_SELECTOR = "#sectionContWide";
  const IMAGE_SELECTOR = '#sectionContWide img[id^="content_image_"]';

  const overlaysByImage = new Map(); // img -> {container, data}
  const registry = new Map(); // boxId -> {boxEl, rowEl}
  let translated = false;
  let panelEls = null;
  let glossary = [];
  let engine = "gemini";
  let detector = "paddleocr";

  function sendMessage(message) {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage(message, (response) => resolve(response));
    });
  }

  function clearOverlay(img) {
    const existing = overlaysByImage.get(img);
    if (existing) {
      existing.container.remove();
    }
    // Drop any translations-list rows left over from a previous render of
    // this same image (initial + re-translate/refresh), so re-rendering
    // never leaves stale duplicate rows behind.
    for (const [boxId, entry] of registry.entries()) {
      if (boxId.startsWith(`${img.id}__b`)) {
        clearTimeout(entry.flashTimer);
        entry.rowEl.remove();
        registry.delete(boxId);
      }
    }
  }

  // Removes a single box (its overlay element + Translations-list row),
  // unlike clearOverlay which drops every box belonging to one whole image.
  // Used when a box's content is being replaced by a differently-shaped
  // box elsewhere (e.g. two boundary-stitched fragments folded into one).
  function removeBox(boxId) {
    const entry = registry.get(boxId);
    if (!entry) return;
    clearTimeout(entry.flashTimer);
    entry.boxEl.remove();
    entry.rowEl.remove();
    registry.delete(boxId);
  }

  // Wrap the image in its own positioning context so the overlay tracks
  // the image's actual box even if panels above it are still loading and
  // shifting the page's layout. Percentage-based box coordinates then need
  // no manual repositioning on resize or reflow.
  function getOrCreateWrap(img) {
    const parent = img.parentElement;
    if (parent && parent.classList.contains("ct-img-wrap")) {
      return parent;
    }
    const wrap = document.createElement("div");
    wrap.className = "ct-img-wrap";
    img.replaceWith(wrap);
    wrap.appendChild(img);
    return wrap;
  }

  function setActive(boxId, active) {
    const entry = registry.get(boxId);
    if (!entry) return;
    entry.boxEl.classList.toggle("ct-active", active);
    entry.rowEl.classList.toggle("ct-active", active);
  }

  // Scroll `el` into view and hold the active highlight briefly so it's
  // obvious which pair lit up, even without hovering.
  function flash(boxId, el) {
    const entry = registry.get(boxId);
    if (!entry) return;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    setActive(boxId, true);
    clearTimeout(entry.flashTimer);
    entry.flashTimer = setTimeout(() => setActive(boxId, false), 1800);
  }

  // Click a row: jump to its bubble on the page.
  function flashHighlight(boxId) {
    const entry = registry.get(boxId);
    if (!entry) return;
    flash(boxId, entry.boxEl);
  }

  // Click a bubble on the page: switch to the Translations tab and jump to
  // its row there.
  function jumpToRow(boxId) {
    const entry = registry.get(boxId);
    if (!entry) return;
    switchTab("translations");
    flash(boxId, entry.rowEl);
  }

  function addTranslationRow(boxId, panelLabel, korean, english, isSfx) {
    const row = document.createElement("div");
    row.className = "ct-trow";
    if (isSfx) row.classList.add("ct-sfx");

    const head = document.createElement("div");
    head.className = "ct-trow-head";

    const panelEl = document.createElement("div");
    panelEl.className = "ct-trow-panel";
    panelEl.textContent = panelLabel;

    const refreshBtn = document.createElement("button");
    refreshBtn.className = "ct-trow-refresh";
    refreshBtn.textContent = "↻";
    refreshBtn.title = "Re-translate just this bubble (use after editing the glossary)";
    refreshBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      refreshBox(boxId);
    });

    head.append(panelEl, refreshBtn);

    const koreanEl = document.createElement("div");
    koreanEl.className = "ct-trow-korean";
    koreanEl.textContent = korean;

    const englishEl = document.createElement("div");
    englishEl.className = "ct-trow-english";
    englishEl.textContent = english;

    row.append(head, koreanEl, englishEl);
    row.addEventListener("mouseenter", () => setActive(boxId, true));
    row.addEventListener("mouseleave", () => setActive(boxId, false));
    row.addEventListener("click", () => flashHighlight(boxId));
    row.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      // Read from the registry, not the closed-over params, so a
      // since-refreshed translation is what actually gets copied.
      const entry = registry.get(boxId);
      showCopyMenu(e.clientX, e.clientY, entry ? entry.korean : korean, entry ? entry.english : english);
    });
    panelEls.translationsList.appendChild(row);
    return row;
  }

  // Shrink `label`'s font until its text fits inside `hit` (down to
  // MIN_FONT_SIZE), instead of letting it clip or spill past the box.
  // Bubble sizes vary panel to panel, so a single global font size can
  // never fit all of them — this makes the "Text size" slider a ceiling
  // rather than a fixed value.
  function fitLabel(hit, label) {
    let size = labelFontSize;
    label.style.fontSize = size + "px";
    while (
      size > MIN_FONT_SIZE &&
      (label.scrollWidth > label.clientWidth + 1 || label.scrollHeight > label.clientHeight + 1)
    ) {
      size -= 1;
      label.style.fontSize = size + "px";
    }
  }

  function renderOverlay(img, data) {
    clearOverlay(img);
    const wrap = getOrCreateWrap(img);

    const container = document.createElement("div");
    container.className = "ct-overlay-container";
    // Attach before populating so each label has real layout to measure
    // against when fitLabel runs (a detached subtree can't compute sizes).
    wrap.appendChild(container);

    data.boxes.forEach((box, i) => {
      const boxId = `${img.id}__b${i}`;

      // Hit area: matches Gemini's detected region exactly (so hovering
      // where the original text was reliably triggers it), but stays
      // invisible other than a faint outline.
      const hit = document.createElement("div");
      hit.className = "ct-bubble";
      if (box.sfx) hit.classList.add("ct-sfx");
      hit.style.left = (box.x / data.width) * 100 + "%";
      hit.style.top = (box.y / data.height) * 100 + "%";
      hit.style.width = (box.w / data.width) * 100 + "%";
      hit.style.height = (box.h / data.height) * 100 + "%";

      // Label: sized to its own text, not stretched to fill the hit area,
      // so short translations don't turn into oversized colored slabs.
      // Background/text color and opacity are user-controlled (see the
      // Text size / Bg opacity / Text color panel controls).
      const label = document.createElement("div");
      label.className = "ct-bubble-label";
      label.textContent = box.english;

      hit.appendChild(label);
      hit.addEventListener("mouseenter", () => setActive(boxId, true));
      hit.addEventListener("mouseleave", () => setActive(boxId, false));
      hit.addEventListener("click", () => jumpToRow(boxId));
      container.appendChild(hit);
      fitLabel(hit, label);

      const panelNum = (img.id.match(/\d+/) || ["?"])[0];
      const row = addTranslationRow(boxId, `#${panelNum}`, box.korean, box.english, box.sfx);
      registry.set(boxId, {
        boxEl: hit,
        rowEl: row,
        labelEl: label,
        korean: box.korean,
        english: box.english,
        flashTimer: null,
      });
    });

    overlaysByImage.set(img, { container, data });
  }

  function setOverlaysVisible(visible) {
    for (const { container } of overlaysByImage.values()) {
      container.style.display = visible ? "" : "none";
    }
  }

  function logStatus(text, isError) {
    const line = document.createElement("div");
    line.textContent = text;
    if (isError) line.style.color = "#ff8a8a";
    panelEls.status.appendChild(line);
    panelEls.status.scrollTop = panelEls.status.scrollHeight;
  }

  // How many panels go into one backend call. The free Gemini tier's daily
  // cap is on request count, so batching several panels per call divides
  // the effective quota cost by roughly this number — matches the
  // backend's own GEMINI_BATCH_SIZE default (5) so the extension's chunks
  // line up with what one Gemini call actually covers.
  const BATCH_SIZE = 5;

  async function translateBatch(imgs, attempt = 1, force = false) {
    const response = await sendMessage({
      type: "translateBatch",
      imageUrls: imgs.map((img) => img.src),
      glossary,
      force,
      engine,
      detector,
    });

    if (!response || !response.ok) {
      const isDailyQuota = response && /daily quota/i.test(response.error || "");
      if (isDailyQuota) {
        // The backend already gave up without retrying (it's not worth
        // it — a daily cap doesn't recover until tomorrow) — don't retry
        // here either, just surface it plainly.
        logStatus("Gemini's daily quota is used up for today. Switch engines or try again tomorrow.", true);
        return;
      }
      if (response && response.status === 429 && attempt < 3) {
        logStatus(`Batch rate limited, retrying in 5s…`);
        await new Promise((r) => setTimeout(r, 5000));
        return translateBatch(imgs, attempt + 1, force);
      }
      logStatus(`Batch failed (${response ? response.status : "no response"})`, true);
      return;
    }

    for (let i = 0; i < response.data.results.length; i++) {
      const result = response.data.results[i];
      const img = imgs[i];
      const label = img.id || img.src.slice(-12);
      if (!result) {
        logStatus(`${label}: failed`, true);
        continue;
      }
      renderOverlay(img, result);
      logStatus(`${label}: ${result.boxes.length} box(es)`);
      // Sequenced (not fire-and-forget): the next image's boundary check
      // needs this image's edge state already updated before it runs.
      await maybeStitchWithPrevious(img, result);
    }
  }

  function applyBoxUpdate(boxId, korean, english) {
    const entry = registry.get(boxId);
    if (!entry) return;
    entry.korean = korean;
    entry.english = english;
    const koreanEl = entry.rowEl.querySelector(".ct-trow-korean");
    if (koreanEl) koreanEl.textContent = korean;
    const englishEl = entry.rowEl.querySelector(".ct-trow-english");
    if (englishEl) englishEl.textContent = english;
    entry.labelEl.textContent = english;
    fitLabel(entry.boxEl, entry.labelEl);
  }

  // --- Cross-image boundary stitching --------------------------------------
  //
  // Naver slices a tall episode into fixed-height image files with no
  // regard for where a speech bubble happens to fall, so a bubble's text
  // can be split across the boundary between two adjacent images — each
  // image gets detected/translated independently, so a split bubble comes
  // back as two disconnected fragments (confirmed directly: "저기술은 대체
  // 어떻게" on one image, "작동하는 거지." on the next, translated as two
  // unrelated sentences instead of one). Since panels render in strict
  // reading order, this catches it after the fact: if one image's
  // bottom-most text sits right at its edge and the next image's top-most
  // text sits right at *its* edge with matching horizontal position,
  // they're almost certainly one sentence — merge and retranslate as a
  // whole, then replace both fragments' boxes with a single box spanning
  // both (not update both in place with the same text — that left two
  // differently-sized boxes both showing the identical full sentence,
  // which read as a duplicate/rendering bug rather than one merged bubble).

  const EDGE_MARGIN = 0.06; // within the first/last 6% of the image's height
  let prevImageBottomEdge = null; // { boxId, korean, img, data, box } | null

  // Converts a box's fractional (percentage-based) image coordinates into a
  // page (document) rect — the same coordinate space renderPageBox expects —
  // using the image's current on-screen position and size.
  function boxToPageRect(img, data, box) {
    const r = img.getBoundingClientRect();
    const left = r.left + (box.x / data.width) * r.width;
    const top = r.top + (box.y / data.height) * r.height;
    const width = (box.w / data.width) * r.width;
    const height = (box.h / data.height) * r.height;
    return {
      left: left + window.scrollX,
      top: top + window.scrollY,
      right: left + width + window.scrollX,
      bottom: top + height + window.scrollY,
    };
  }

  function unionPageRect(a, b) {
    const left = Math.min(a.left, b.left);
    const top = Math.min(a.top, b.top);
    const right = Math.max(a.right, b.right);
    const bottom = Math.max(a.bottom, b.bottom);
    return { left, top, width: right - left, height: bottom - top };
  }

  async function maybeStitchWithPrevious(img, data) {
    if (!data.boxes.length) {
      prevImageBottomEdge = null;
      return;
    }

    let topIdx = 0;
    let bottomIdx = 0;
    data.boxes.forEach((b, i) => {
      if (b.y < data.boxes[topIdx].y) topIdx = i;
      if (b.y + b.h > data.boxes[bottomIdx].y + data.boxes[bottomIdx].h) bottomIdx = i;
    });
    const top = data.boxes[topIdx];
    const bottom = data.boxes[bottomIdx];
    const topBoxId = `${img.id}__b${topIdx}`;
    const bottomBoxId = `${img.id}__b${bottomIdx}`;

    const topIsNearEdge = top.y < data.height * EDGE_MARGIN;
    const bottomIsNearEdge = bottom.y + bottom.h > data.height * (1 - EDGE_MARGIN);

    if (prevImageBottomEdge && topIsNearEdge) {
      const xOverlap =
        Math.min(prevImageBottomEdge.box.x + prevImageBottomEdge.box.w, top.x + top.w) -
        Math.max(prevImageBottomEdge.box.x, top.x);
      const narrower = Math.min(prevImageBottomEdge.box.w, top.w);

      if (narrower > 0 && xOverlap > 0.3 * narrower) {
        const mergedKorean = `${prevImageBottomEdge.korean} ${top.korean}`;
        // Captured now, before the network round-trip below, same reasoning
        // as the manual-selection tool: if the page scrolls while we wait,
        // the merged box should still land exactly on the two fragments'
        // original spot, not drift with the scroll.
        const unionRect = unionPageRect(
          boxToPageRect(prevImageBottomEdge.img, prevImageBottomEdge.data, prevImageBottomEdge.box),
          boxToPageRect(img, data, top)
        );
        const response = await sendMessage({ type: "retranslate", korean: mergedKorean, glossary, engine });
        if (response && response.ok) {
          removeBox(prevImageBottomEdge.boxId);
          removeBox(topBoxId);
          renderPageBox(unionRect, mergedKorean, response.data.english, {
            idPrefix: "stitch",
            boxClassName: "ct-stitched-bubble",
            panelLabel: "Stitched",
          });
          logStatus(`Stitched dialogue split across ${img.id}'s boundary.`);

          // The top fragment (now removed) was this image's only box, so
          // there's no real "bottom edge" left in it to chain a further
          // stitch against a third image onto — carrying its pre-merge text
          // forward would silently drop the merge that just happened.
          if (topBoxId === bottomBoxId) {
            prevImageBottomEdge = null;
            return;
          }
        }
      }
    }

    prevImageBottomEdge = bottomIsNearEdge
      ? { boxId: bottomBoxId, korean: bottom.korean, img, data, box: bottom }
      : null;
  }

  // --- Manual region selection ----------------------------------------------
  //
  // For gaps automated detection misses entirely (e.g. a line split mid-row
  // across an image boundary — not caught by the boundary stitch above,
  // since that needs both fragments to already be valid detections; here
  // neither one is). Drag a selection over the page; the backend crops the
  // overlapping image(s) server-side — client-side canvas cropping is
  // blocked, since Naver's CDN sends no CORS headers — and treats the whole
  // selection as one translation unit, however many images it spans.

  let selecting = false;
  let selectStart = null; // {x, y} in viewport coordinates
  let selectBoxEl = null; // the live drag-rectangle shown while dragging
  let pageBoxCounter = 0;

  function startSelectMode() {
    selecting = true;
    document.body.classList.add("ct-selecting");
    panelEls.selectBtn.textContent = "Cancel Selection (Esc)";
    panelEls.selectBtn.classList.add("ct-active-toggle");
    logStatus("Drag over the page to select an area to translate.");
  }

  function stopSelectMode() {
    selecting = false;
    document.body.classList.remove("ct-selecting");
    panelEls.selectBtn.textContent = "Select Area to Translate";
    panelEls.selectBtn.classList.remove("ct-active-toggle");
    if (selectBoxEl) {
      selectBoxEl.remove();
      selectBoxEl = null;
    }
    selectStart = null;
  }

  function updateSelectBox(x, y) {
    if (!selectBoxEl || !selectStart) return;
    selectBoxEl.style.left = Math.min(selectStart.x, x) + "px";
    selectBoxEl.style.top = Math.min(selectStart.y, y) + "px";
    selectBoxEl.style.width = Math.abs(x - selectStart.x) + "px";
    selectBoxEl.style.height = Math.abs(y - selectStart.y) + "px";
  }

  function onSelectMouseDown(e) {
    if (!selecting || e.target.closest(".ct-panel")) return;
    e.preventDefault();
    selectStart = { x: e.clientX, y: e.clientY };
    selectBoxEl = document.createElement("div");
    selectBoxEl.className = "ct-select-box";
    document.body.appendChild(selectBoxEl);
    updateSelectBox(e.clientX, e.clientY);
  }

  function onSelectMouseMove(e) {
    if (!selecting || !selectStart) return;
    updateSelectBox(e.clientX, e.clientY);
  }

  async function onSelectMouseUp(e) {
    if (!selecting || !selectStart) return;
    const rect = {
      left: Math.min(selectStart.x, e.clientX),
      top: Math.min(selectStart.y, e.clientY),
      right: Math.max(selectStart.x, e.clientX),
      bottom: Math.max(selectStart.y, e.clientY),
    };
    stopSelectMode();

    if (rect.right - rect.left < 8 || rect.bottom - rect.top < 8) {
      return; // an accidental click, not a real drag
    }
    await translateSelection(rect);
  }

  document.addEventListener("mousedown", onSelectMouseDown, true);
  document.addEventListener("mousemove", onSelectMouseMove, true);
  document.addEventListener("mouseup", onSelectMouseUp, true);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && selecting) stopSelectMode();
  });

  async function translateSelection(viewportRect) {
    // Page (document) coordinates, not viewer-relative ones: captured now,
    // before the network round-trip below, so the rendered box still lands
    // exactly where the user dragged even if the page scrolls while we wait.
    // #sectionContWide (VIEWER_SELECTOR) isn't itself a CSS positioning
    // context, so a position:absolute box appended inside it and positioned
    // with viewer-relative left/top escapes past it to the document root —
    // that's what put the box up near the top of the page instead of at the
    // drag location. Document coordinates + appending to document.body sidestep
    // that: they're correct regardless of what #sectionContWide's own
    // position is, and scroll-invariant the same way the old approach intended.
    const renderRect = {
      left: viewportRect.left + window.scrollX,
      top: viewportRect.top + window.scrollY,
      width: viewportRect.right - viewportRect.left,
      height: viewportRect.bottom - viewportRect.top,
    };

    const images = Array.from(document.querySelectorAll(IMAGE_SELECTOR));
    const crops = [];
    for (const img of images) {
      const r = img.getBoundingClientRect();
      const overlapLeft = Math.max(viewportRect.left, r.left);
      const overlapTop = Math.max(viewportRect.top, r.top);
      const overlapRight = Math.min(viewportRect.right, r.right);
      const overlapBottom = Math.min(viewportRect.bottom, r.bottom);
      if (overlapRight <= overlapLeft || overlapBottom <= overlapTop) continue;

      const scaleX = img.naturalWidth / r.width;
      const scaleY = img.naturalHeight / r.height;
      crops.push({
        image_url: img.src,
        x: Math.round((overlapLeft - r.left) * scaleX),
        y: Math.round((overlapTop - r.top) * scaleY),
        w: Math.round((overlapRight - overlapLeft) * scaleX),
        h: Math.round((overlapBottom - overlapTop) * scaleY),
      });
    }

    if (crops.length === 0) {
      logStatus("Selection didn't overlap any panel image.", true);
      return;
    }

    logStatus(`Translating selected area (${crops.length} panel${crops.length > 1 ? "s" : ""})…`);
    const response = await sendMessage({ type: "translateRegion", crops, glossary, engine, detector });

    if (!response || !response.ok) {
      logStatus(`Selection translate failed (${response ? response.status : "no response"})`, true);
      return;
    }

    const { korean, english } = response.data;
    if (!korean) {
      logStatus("No text found in that selection.", true);
      return;
    }

    renderManualBox(renderRect, korean, english);
    logStatus(`Selection translated: "${english}"`);
  }

  // Renders one box positioned in page (document) coordinates, appended to
  // <body> instead of a specific image's .ct-img-wrap — for boxes that don't
  // belong to a single source image, either because the user drew an
  // arbitrary selection (manual) or because it replaces two per-image
  // fragments that got folded into one (stitched). renderRect is
  // {left, top, width, height} in page coordinates.
  function renderPageBox(renderRect, korean, english, { idPrefix, boxClassName, panelLabel }) {
    const boxId = `${idPrefix}__${pageBoxCounter++}`;

    const hit = document.createElement("div");
    hit.className = `ct-bubble ${boxClassName}`;
    hit.style.position = "absolute";
    hit.style.left = renderRect.left + "px";
    hit.style.top = renderRect.top + "px";
    hit.style.width = renderRect.width + "px";
    hit.style.height = renderRect.height + "px";

    const label = document.createElement("div");
    label.className = "ct-bubble-label";
    label.textContent = english;
    hit.appendChild(label);

    hit.addEventListener("mouseenter", () => setActive(boxId, true));
    hit.addEventListener("mouseleave", () => setActive(boxId, false));
    hit.addEventListener("click", () => jumpToRow(boxId));

    document.body.appendChild(hit);
    fitLabel(hit, label);

    const row = addTranslationRow(boxId, panelLabel, korean, english, false);
    registry.set(boxId, { boxEl: hit, rowEl: row, labelEl: label, korean, english, flashTimer: null });
    return boxId;
  }

  function renderManualBox(renderRect, korean, english) {
    renderPageBox(renderRect, korean, english, {
      idPrefix: "manual",
      boxClassName: "ct-manual-bubble",
      panelLabel: "Manual",
    });
  }

  // Re-translate just one bubble's already-detected Korean text, leaving
  // its box, position, and every other bubble on the panel untouched.
  async function refreshBox(boxId) {
    const entry = registry.get(boxId);
    if (!entry) return;

    const btn = entry.rowEl.querySelector(".ct-trow-refresh");
    if (btn) btn.disabled = true;

    const response = await sendMessage({ type: "retranslate", korean: entry.korean, glossary, engine });

    if (btn) btn.disabled = false;

    if (!response || !response.ok) {
      logStatus(`Retranslate failed (${response ? response.status : "no response"})`, true);
      return;
    }

    applyBoxUpdate(boxId, entry.korean, response.data.english);
  }

  function updateProgress(done, total) {
    panelEls.progressFill.style.width = total ? `${(done / total) * 100}%` : "0%";
    panelEls.progressText.textContent = `${done} / ${total} panels`;
  }

  async function translateEpisode() {
    const images = Array.from(document.querySelectorAll(IMAGE_SELECTOR));
    if (images.length === 0) {
      logStatus("No panel images found on this page.", true);
      return;
    }

    prevImageBottomEdge = null; // don't stitch against a previous run's leftover state

    // PaddleOCR has no quota to conserve, so there's no reason to batch it —
    // chunking at 1 gives a progress update after every single panel instead
    // of every 5. Gemini keeps real batching since that's what divides its
    // request-count cost.
    const chunkSize = detector === "gemini" ? BATCH_SIZE : 1;

    panelEls.translateBtn.disabled = true;
    panelEls.status.innerHTML = "";
    panelEls.translationsList.innerHTML = "";
    registry.clear();
    logStatus(
      chunkSize > 1
        ? `Found ${images.length} panels. Translating in batches of ${chunkSize}…`
        : `Found ${images.length} panels. Translating…`
    );

    const total = images.length;
    let done = 0;
    panelEls.progressWrap.style.display = "block";
    updateProgress(done, total);

    for (let start = 0; start < images.length; start += chunkSize) {
      const chunk = images.slice(start, start + chunkSize);
      await translateBatch(chunk);
      done += chunk.length;
      updateProgress(done, total);
    }

    translated = true;
    panelEls.translateBtn.disabled = false;
    panelEls.translateBtn.textContent = "Re-translate";
    panelEls.toggleBtn.style.display = "block";
    logStatus("Done.");
  }

  function toggleOverlays() {
    const anyVisible =
      overlaysByImage.size > 0 &&
      [...overlaysByImage.values()][0].container.style.display !== "none";
    setOverlaysVisible(!anyVisible);
    panelEls.toggleBtn.textContent = anyVisible ? "Show Translation" : "Hide Translation";
  }

  // --- Glossary ---------------------------------------------------------

  function loadGlossary() {
    chrome.storage.local.get(["ctGlossary"], (result) => {
      glossary = result.ctGlossary || [];
      renderGlossaryList();
    });
  }

  function saveGlossary() {
    chrome.storage.local.set({ ctGlossary: glossary });
  }

  function renderGlossaryList() {
    const list = panelEls.glossaryList;
    list.innerHTML = "";
    if (glossary.length === 0) {
      const empty = document.createElement("div");
      empty.className = "ct-glossary-empty";
      empty.textContent = "No terms yet. Add a name below to keep it consistent across panels.";
      list.appendChild(empty);
      return;
    }
    glossary.forEach((entry, i) => {
      const row = document.createElement("div");
      row.className = "ct-glossary-row";

      const kr = document.createElement("span");
      kr.className = "ct-glossary-kr";
      kr.textContent = entry.korean;

      const arrow = document.createElement("span");
      arrow.className = "ct-glossary-arrow";
      arrow.textContent = "→";

      const en = document.createElement("span");
      en.className = "ct-glossary-en";
      en.textContent = entry.english;

      const del = document.createElement("button");
      del.className = "ct-glossary-del";
      del.textContent = "×";
      del.addEventListener("click", () => {
        glossary.splice(i, 1);
        saveGlossary();
        renderGlossaryList();
      });

      row.append(kr, arrow, en, del);
      list.appendChild(row);
    });
  }

  function addGlossaryEntry() {
    const kr = panelEls.glossaryKr.value.trim();
    const en = panelEls.glossaryEn.value.trim();
    if (!kr || !en) return;
    glossary.push({ korean: kr, english: en });
    panelEls.glossaryKr.value = "";
    panelEls.glossaryEn.value = "";
    saveGlossary();
    renderGlossaryList();
  }

  // --- Translation engine ----------------------------------------------

  const VALID_ENGINES = ["gemini", "azure", "deepl", "nllb"];

  function loadEngine() {
    chrome.storage.local.get(["ctEngine"], (result) => {
      // Falls back to "gemini" for a stored value from a since-removed
      // engine (e.g. the old Papago/Argos options), not just a missing one.
      engine = VALID_ENGINES.includes(result.ctEngine) ? result.ctEngine : "gemini";
      if (panelEls) panelEls.engineSelect.value = engine;
    });
  }

  function changeEngine(value) {
    engine = value;
    chrome.storage.local.set({ ctEngine: value });
  }

  // --- Detector (finding boxes + reading Korean) ------------------------
  //
  // Independent of Engine (which only handles English translation).
  // PaddleOCR is free/local/no quota but only reliable on real dialogue —
  // see the Detector dropdown's option text. Gemini remains available for
  // full SFX coverage or when PaddleOCR's confidence filtering misses text.

  function loadDetector() {
    chrome.storage.local.get(["ctDetector"], (result) => {
      detector = result.ctDetector || "paddleocr";
      if (panelEls) panelEls.detectorSelect.value = detector;
    });
  }

  function changeDetector(value) {
    detector = value;
    chrome.storage.local.set({ ctDetector: value });
  }

  // --- SFX visibility -----------------------------------------------------
  //
  // Purely a display filter — SFX boxes are still detected and translated
  // (Gemini has to look at the whole panel either way), this just hides
  // them client-side so they don't clutter the page/list. Toggling it never
  // needs a re-translate.

  let showSfx = false;

  function applyShowSfx() {
    document.documentElement.classList.toggle("ct-hide-sfx", !showSfx);
    if (panelEls) panelEls.sfxCheckbox.checked = showSfx;
  }

  function loadShowSfx() {
    chrome.storage.local.get(["ctShowSfx"], (result) => {
      showSfx = result.ctShowSfx ?? false;
      applyShowSfx();
    });
  }

  function changeShowSfx(value) {
    showSfx = value;
    chrome.storage.local.set({ ctShowSfx: value });
    applyShowSfx();
  }

  // --- Label visibility (always-shown vs. invisible-until-hover) ---------

  let alwaysShowLabels = true;

  function applyAlwaysShowLabels() {
    document.documentElement.classList.toggle("ct-labels-hover-only", !alwaysShowLabels);
    if (panelEls) panelEls.showLabelsCheckbox.checked = alwaysShowLabels;
  }

  function loadAlwaysShowLabels() {
    chrome.storage.local.get(["ctAlwaysShowLabels"], (result) => {
      alwaysShowLabels = result.ctAlwaysShowLabels ?? true;
      applyAlwaysShowLabels();
    });
  }

  function changeAlwaysShowLabels(value) {
    alwaysShowLabels = value;
    chrome.storage.local.set({ ctAlwaysShowLabels: value });
    applyAlwaysShowLabels();
  }

  // --- Label appearance (size / background opacity / text color) -----------

  let labelFontSize = 24;
  const MIN_FONT_SIZE = 10;
  const MAX_FONT_SIZE = 40;

  function applyFontSize() {
    document.documentElement.style.setProperty("--ct-label-font-size", labelFontSize + "px");
    if (panelEls) panelEls.fontVal.textContent = labelFontSize + "px";
  }

  function loadFontSize() {
    chrome.storage.local.get(["ctFontSize"], (result) => {
      labelFontSize = result.ctFontSize ?? 24;
      applyFontSize();
    });
  }

  function refitAllLabels() {
    for (const entry of registry.values()) {
      if (entry.labelEl) fitLabel(entry.boxEl, entry.labelEl);
    }
  }

  function changeFontSize(delta) {
    labelFontSize = Math.min(MAX_FONT_SIZE, Math.max(MIN_FONT_SIZE, labelFontSize + delta));
    chrome.storage.local.set({ ctFontSize: labelFontSize });
    applyFontSize();
    refitAllLabels();
  }

  function applyOpacity(percent) {
    document.documentElement.style.setProperty("--ct-label-bg-opacity", percent / 100);
    if (panelEls) panelEls.opacityVal.textContent = percent + "%";
  }

  function loadOpacity() {
    chrome.storage.local.get(["ctLabelOpacity"], (result) => {
      const percent = result.ctLabelOpacity ?? 100;
      if (panelEls) panelEls.opacityRange.value = percent;
      applyOpacity(percent);
    });
  }

  function changeOpacity(percent) {
    chrome.storage.local.set({ ctLabelOpacity: percent });
    applyOpacity(percent);
  }

  function applyLabelColor(hex) {
    document.documentElement.style.setProperty("--ct-label-color", hex);
  }

  function loadLabelColor() {
    chrome.storage.local.get(["ctLabelColor"], (result) => {
      const hex = result.ctLabelColor || "#111111";
      if (panelEls) panelEls.colorInput.value = hex;
      applyLabelColor(hex);
    });
  }

  function changeLabelColor(hex) {
    chrome.storage.local.set({ ctLabelColor: hex });
    applyLabelColor(hex);
  }

  // --- Copy menu (right-click a Translations row) --------------------------

  let copyMenuEl = null;

  function buildCopyMenu() {
    const menu = document.createElement("div");
    menu.className = "ct-copy-menu";

    const copyKr = document.createElement("button");
    copyKr.dataset.action = "korean";
    copyKr.textContent = "Copy Korean";

    const copyEn = document.createElement("button");
    copyEn.dataset.action = "english";
    copyEn.textContent = "Copy English";

    menu.append(copyKr, copyEn);
    document.body.appendChild(menu);
    return menu;
  }

  function hideCopyMenu() {
    if (copyMenuEl) copyMenuEl.classList.remove("open");
  }

  function showCopyMenu(x, y, korean, english) {
    if (!copyMenuEl) copyMenuEl = buildCopyMenu();
    copyMenuEl.querySelector('[data-action="korean"]').onclick = () => {
      navigator.clipboard.writeText(korean);
      hideCopyMenu();
    };
    copyMenuEl.querySelector('[data-action="english"]').onclick = () => {
      navigator.clipboard.writeText(english);
      hideCopyMenu();
    };
    // Keep it on-screen if the click happened near the panel's edge.
    const menuWidth = 160;
    const left = Math.min(x, window.innerWidth - menuWidth - 8);
    copyMenuEl.style.left = left + "px";
    copyMenuEl.style.top = y + "px";
    copyMenuEl.classList.add("open");
  }

  document.addEventListener("click", hideCopyMenu);
  document.addEventListener("scroll", hideCopyMenu, true);

  // --- Panel UI -----------------------------------------------------------

  function switchTab(tabName) {
    for (const btn of panelEls.tabButtons) {
      btn.classList.toggle("active", btn.dataset.tab === tabName);
    }
    for (const [name, el] of Object.entries(panelEls.tabContents)) {
      el.style.display = name === tabName ? "" : "none";
    }
  }

  function buildPanel() {
    const panel = document.createElement("div");
    panel.className = "ct-panel";
    panel.innerHTML = `
      <h3><span class="ct-dot bad" id="ct-health-dot"></span>Webtoon Translator</h3>
      <button id="ct-translate-btn">Translate Episode</button>
      <button id="ct-toggle-btn" style="display:none">Hide Translation</button>
      <button id="ct-select-btn" class="ct-secondary-btn">Select Area to Translate</button>
      <div class="ct-progress-wrap" id="ct-progress-wrap" style="display:none">
        <div class="ct-progress-bar"><div class="ct-progress-fill" id="ct-progress-fill"></div></div>
        <div class="ct-progress-text" id="ct-progress-text">0 / 0 panels</div>
      </div>
      <div class="ct-fontsize-row">
        <span>Text size</span>
        <button id="ct-font-dec">–</button>
        <span id="ct-font-val">24px</span>
        <button id="ct-font-inc">+</button>
      </div>
      <div class="ct-fontsize-row">
        <span>Bg opacity</span>
        <input type="range" id="ct-opacity-range" min="0" max="100" value="100" />
        <span id="ct-opacity-val">100%</span>
      </div>
      <div class="ct-fontsize-row">
        <span>Text color</span>
        <input type="color" id="ct-color-input" value="#111111" />
      </div>
      <div class="ct-fontsize-row">
        <span>Always show translation</span>
        <input type="checkbox" id="ct-always-show-checkbox" />
      </div>
      <div class="ct-fontsize-row">
        <span>Show sound effects</span>
        <input type="checkbox" id="ct-sfx-checkbox" />
      </div>
      <div class="ct-fontsize-row">
        <span>Detector</span>
        <select id="ct-detector-select">
          <option value="paddleocr">PaddleOCR (free, dialogue only)</option>
          <option value="gemini">Gemini (best, full SFX coverage)</option>
        </select>
      </div>
      <div class="ct-fontsize-row">
        <span>Engine</span>
        <select id="ct-engine-select">
          <option value="gemini">Gemini</option>
          <option value="azure">Azure</option>
          <option value="deepl">DeepL</option>
          <option value="nllb">NLLB (free, offline)</option>
        </select>
      </div>
      <div class="ct-tabs">
        <button class="ct-tab active" data-tab="log">Log</button>
        <button class="ct-tab" data-tab="translations">Translations</button>
        <button class="ct-tab" data-tab="glossary">Glossary</button>
      </div>
      <div class="ct-tab-content" id="ct-tab-log">
        <div class="ct-status" id="ct-status"></div>
      </div>
      <div class="ct-tab-content" id="ct-tab-translations" style="display:none">
        <div class="ct-translations-list" id="ct-translations-list"></div>
      </div>
      <div class="ct-tab-content" id="ct-tab-glossary" style="display:none">
        <div class="ct-glossary-form">
          <input id="ct-gloss-kr" type="text" placeholder="Korean" />
          <input id="ct-gloss-en" type="text" placeholder="English" />
          <button id="ct-gloss-add">Add</button>
        </div>
        <div class="ct-glossary-list" id="ct-glossary-list"></div>
      </div>
    `;
    document.body.appendChild(panel);

    const translateBtn = panel.querySelector("#ct-translate-btn");
    const toggleBtn = panel.querySelector("#ct-toggle-btn");
    const selectBtn = panel.querySelector("#ct-select-btn");
    const progressWrap = panel.querySelector("#ct-progress-wrap");
    const progressFill = panel.querySelector("#ct-progress-fill");
    const progressText = panel.querySelector("#ct-progress-text");
    const status = panel.querySelector("#ct-status");
    const healthDot = panel.querySelector("#ct-health-dot");
    const translationsList = panel.querySelector("#ct-translations-list");
    const glossaryList = panel.querySelector("#ct-glossary-list");
    const glossaryKr = panel.querySelector("#ct-gloss-kr");
    const glossaryEn = panel.querySelector("#ct-gloss-en");
    const fontVal = panel.querySelector("#ct-font-val");
    const opacityRange = panel.querySelector("#ct-opacity-range");
    const opacityVal = panel.querySelector("#ct-opacity-val");
    const colorInput = panel.querySelector("#ct-color-input");
    const detectorSelect = panel.querySelector("#ct-detector-select");
    const engineSelect = panel.querySelector("#ct-engine-select");
    const showLabelsCheckbox = panel.querySelector("#ct-always-show-checkbox");
    const sfxCheckbox = panel.querySelector("#ct-sfx-checkbox");
    const tabButtons = Array.from(panel.querySelectorAll(".ct-tab"));
    const tabContents = {
      log: panel.querySelector("#ct-tab-log"),
      translations: panel.querySelector("#ct-tab-translations"),
      glossary: panel.querySelector("#ct-tab-glossary"),
    };

    translateBtn.addEventListener("click", translateEpisode);
    toggleBtn.addEventListener("click", toggleOverlays);
    selectBtn.addEventListener("click", () => {
      if (selecting) {
        stopSelectMode();
      } else {
        startSelectMode();
      }
    });
    panel.querySelector("#ct-gloss-add").addEventListener("click", addGlossaryEntry);
    panel.querySelector("#ct-font-dec").addEventListener("click", () => changeFontSize(-1));
    panel.querySelector("#ct-font-inc").addEventListener("click", () => changeFontSize(1));
    opacityRange.addEventListener("input", () => changeOpacity(Number(opacityRange.value)));
    colorInput.addEventListener("input", () => changeLabelColor(colorInput.value));
    engineSelect.addEventListener("change", () => changeEngine(engineSelect.value));
    detectorSelect.addEventListener("change", () => changeDetector(detectorSelect.value));
    showLabelsCheckbox.addEventListener("change", () => changeAlwaysShowLabels(showLabelsCheckbox.checked));
    sfxCheckbox.addEventListener("change", () => changeShowSfx(sfxCheckbox.checked));
    for (const btn of tabButtons) {
      btn.addEventListener("click", () => switchTab(btn.dataset.tab));
    }

    panelEls = {
      translateBtn,
      toggleBtn,
      selectBtn,
      progressWrap,
      progressFill,
      progressText,
      status,
      healthDot,
      translationsList,
      glossaryList,
      glossaryKr,
      glossaryEn,
      fontVal,
      opacityRange,
      opacityVal,
      colorInput,
      detectorSelect,
      engineSelect,
      showLabelsCheckbox,
      sfxCheckbox,
      tabButtons,
      tabContents,
    };

    loadGlossary();
    loadFontSize();
    loadOpacity();
    loadLabelColor();
    loadDetector();
    loadEngine();
    loadAlwaysShowLabels();
    loadShowSfx();

    sendMessage({ type: "health" }).then((response) => {
      if (response && response.ok) {
        healthDot.classList.replace("bad", "ok");
      } else {
        logStatus("Backend not reachable at 127.0.0.1:8000 — start it with `python main.py`.", true);
      }
    });
  }

  buildPanel();
})();
