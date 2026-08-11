const BACKEND_URL = "http://127.0.0.1:8000";

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type === "translate") {
    fetch(`${BACKEND_URL}/translate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        image_url: message.imageUrl,
        glossary: message.glossary || [],
        force: message.force || false,
        engine: message.engine || "gemini",
        detector: message.detector || "paddleocr",
      }),
    })
      .then(async (res) => {
        if (!res.ok) {
          const detail = await res.text().catch(() => "");
          sendResponse({ ok: false, status: res.status, error: detail });
          return;
        }
        const data = await res.json();
        sendResponse({ ok: true, data });
      })
      .catch((err) => sendResponse({ ok: false, status: 0, error: String(err) }));
    return true;
  }

  if (message.type === "translateBatch") {
    fetch(`${BACKEND_URL}/translate_batch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        image_urls: message.imageUrls,
        glossary: message.glossary || [],
        force: message.force || false,
        engine: message.engine || "gemini",
        detector: message.detector || "paddleocr",
      }),
    })
      .then(async (res) => {
        if (!res.ok) {
          const detail = await res.text().catch(() => "");
          sendResponse({ ok: false, status: res.status, error: detail });
          return;
        }
        const data = await res.json();
        sendResponse({ ok: true, data });
      })
      .catch((err) => sendResponse({ ok: false, status: 0, error: String(err) }));
    return true;
  }

  if (message.type === "translateRegion") {
    fetch(`${BACKEND_URL}/translate_region`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        crops: message.crops,
        glossary: message.glossary || [],
        engine: message.engine || "gemini",
        detector: message.detector || "paddleocr",
      }),
    })
      .then(async (res) => {
        if (!res.ok) {
          const detail = await res.text().catch(() => "");
          sendResponse({ ok: false, status: res.status, error: detail });
          return;
        }
        const data = await res.json();
        sendResponse({ ok: true, data });
      })
      .catch((err) => sendResponse({ ok: false, status: 0, error: String(err) }));
    return true;
  }

  if (message.type === "retranslate") {
    fetch(`${BACKEND_URL}/retranslate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        korean: message.korean,
        glossary: message.glossary || [],
        engine: message.engine || "gemini",
      }),
    })
      .then(async (res) => {
        if (!res.ok) {
          const detail = await res.text().catch(() => "");
          sendResponse({ ok: false, status: res.status, error: detail });
          return;
        }
        const data = await res.json();
        sendResponse({ ok: true, data });
      })
      .catch((err) => sendResponse({ ok: false, status: 0, error: String(err) }));
    return true;
  }

  if (message.type === "health") {
    fetch(`${BACKEND_URL}/health`)
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error(`HTTP ${res.status}`))))
      .then((data) => sendResponse({ ok: true, data }))
      .catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true;
  }

  return false;
});
