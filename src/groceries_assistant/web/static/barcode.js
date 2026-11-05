// Barcode scanning for the settings, plan and order pages: decodes a
// package's EAN and fills the product name (settings) or adds the package to
// the shopping list (/plan, /order). An unknown barcode asks for the name
// once and is remembered server-side, so the next scan of the same package
// fills instantly.
//
// Decoding tiers, per browser:
// - Chromium: the built-in BarcodeDetector on a live camera stream;
// - Firefox e.a. on a secure context (localhost): a live stream decoded by
//   the vendored ZXing library;
// - Firefox e.a. on plain http (a phone on the LAN, where getUserMedia is
//   blocked): photograph the barcode with the native camera app
//   (capture="environment") and decode the image with ZXing;
// - a failing/cancelled scan falls back to typing the EAN.
//
// The product name itself always comes from the server's registry
// (data/barcodes.yaml): known barcodes fill immediately, unknown ones are
// asked for once.
(function () {
  function lookup(ean) {
    return fetch("/barcode/" + ean).then((r) => (r.ok ? r.json() : null));
  }

  function remember(ean, title) {
    return fetch("/barcode/" + ean, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ title: title }),
    }).then((r) => (r.ok ? r.json() : null));
  }

  function loadZxing() {
    if (window.ZXing) return Promise.resolve(window.ZXing);
    return new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "/static/zxing.min.js";
      script.onload = () =>
        window.ZXing ? resolve(window.ZXing) : reject(new Error("scanbibliotheek kapot"));
      script.onerror = () => reject(new Error("scanbibliotheek niet geladen"));
      document.head.appendChild(script);
    });
  }

  function scanOverlay(videoStyle) {
    // A small live preview bottom-right plus a cancel button; returns
    // { video, cancel } and a remove() to clean both up.
    const video = document.createElement("video");
    video.setAttribute("playsinline", "");
    video.muted = true;
    video.style.cssText = videoStyle;
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.textContent = "annuleer";
    cancel.style.cssText =
      "position:fixed;bottom:1rem;left:1rem;z-index:60;background:var(--surface,#eee);";
    document.body.append(video, cancel);
    return {
      video,
      cancel,
      remove() {
        video.remove();
        cancel.remove();
      },
    };
  }

  const CANCELLED = null;

  async function scanLiveDetector() {
    // Chromium: BarcodeDetector on a hidden stream.
    const stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "environment" },
    });
    const overlay = scanOverlay("display:none;");
    let cancelled = false;
    overlay.cancel.addEventListener("click", () => {
      cancelled = true;
    });
    const video = overlay.video;
    video.srcObject = stream;
    document.body.appendChild(video);
    await video.play();
    const detector = new BarcodeDetector();
    try {
      for (;;) {
        if (cancelled) return CANCELLED;
        const codes = await detector.detect(video);
        if (codes.length) return codes[0].rawValue;
        await new Promise((resolve) => setTimeout(resolve, 250));
      }
    } finally {
      stream.getTracks().forEach((track) => track.stop());
      overlay.remove();
    }
  }

  async function scanLiveZxing() {
    // Firefox e.a. on a secure context: live stream + ZXing.
    const ZXing = await loadZxing();
    const overlay = scanOverlay(
      "position:fixed;bottom:1rem;right:1rem;width:min(60vw,320px);" +
        "z-index:60;background:#000;border-radius:8px;"
    );
    const reader = new ZXing.BrowserMultiFormatReader();
    const cleanup = () => {
      reader.reset(); // stops the camera
      overlay.remove();
    };
    overlay.cancel.addEventListener("click", () => cleanup());
    return await new Promise((resolve, reject) => {
      reader
        .decodeFromVideoDevice(null, overlay.video, (result, error) => {
          if (result) {
            cleanup();
            resolve(result.getText());
          } else if (error && error.name && error.name !== "NotFoundException") {
            cleanup();
            reject(new Error("camera-leesfout"));
          }
          // NotFoundException: this frame held no barcode; keep scanning
        })
        .catch((streamError) => {
          cleanup();
          reject(streamError);
        });
    });
  }

  async function scanPhotoZxing() {
    // Plain-http phones: getUserMedia is blocked there, but the native
    // camera app is not (capture="environment"); decode the photo instead.
    const ZXing = await loadZxing();
    return await new Promise((resolve, reject) => {
      const input = document.createElement("input");
      input.type = "file";
      input.accept = "image/*";
      input.capture = "environment";
      input.style.display = "none";
      document.body.appendChild(input);
      input.addEventListener("change", async () => {
        const file = input.files && input.files[0];
        input.remove();
        if (!file) return resolve(CANCELLED); // the camera app was closed
        const url = URL.createObjectURL(file);
        const reader = new ZXing.BrowserMultiFormatReader();
        try {
          const result = await reader.decodeFromImageUrl(url);
          resolve(result ? result.getText() : CANCELLED);
        } catch (error) {
          reject(new Error("geen barcode in de foto — iets dichter bij?"));
        } finally {
          reader.reset();
          URL.revokeObjectURL(url);
        }
      });
      input.click();
    });
  }

  async function scan() {
    if ("BarcodeDetector" in window) return await scanLiveDetector();
    if (window.isSecureContext) return await scanLiveZxing();
    return await scanPhotoZxing();
  }

  async function scanOrType() {
    try {
      const ean = (await scan() || "").trim();
      return ean || null; // a cancelled scan stays cancelled
    } catch (error) {
      console.warn("scannen mislukt:", error.message);
      return window.prompt(
        `Scannen mislukt (${error.message}); typ de EAN (alleen cijfers):`
      );
    }
  }

  for (const button of document.querySelectorAll("[data-barcode-scan]")) {
    button.addEventListener("click", async () => {
      const input = document.getElementById(button.dataset.barcodeScan);
      if (!input) return;
      try {
        const ean = (await scanOrType() || "").trim();
        if (!ean) return; // cancelled
        const known = await lookup(ean);
        if (known) {
          input.value = known.title;
          return;
        }
        const title = window.prompt(
          "Onbekende barcode " + ean + " — welke productnaam is dit?"
        );
        if (!title) return;
        await remember(ean, title);
        input.value = title.trim();
      } catch (error) {
        alert("Scannen mislukt: " + error.message);
      }
    });
  }

  // Add-mode (the scan button on /plan and /order): scan, teach the name if
  // needed, then submit the hidden form so the server adds the package to
  // the week's shopping list and reloads the page with the result.
  for (const button of document.querySelectorAll("[data-barcode-add]")) {
    button.addEventListener("click", async () => {
      const form = document.getElementById(button.dataset.barcodeAdd);
      if (!form) return;
      try {
        const ean = (await scanOrType() || "").trim();
        if (!ean) return; // cancelled
        const known = await lookup(ean);
        if (!known) {
          const title = window.prompt(
            "Onbekende barcode " + ean + " — welke productnaam is dit?"
          );
          if (!title) return;
          await remember(ean, title);
        }
        form.ean.value = ean;
        form.submit();
      } catch (error) {
        alert("Scannen mislukt: " + error.message);
      }
    });
  }
})();
