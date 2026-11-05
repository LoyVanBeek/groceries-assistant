// Barcode scanning for the settings forms: decodes a package's EAN with the
// browser's BarcodeDetector (Chrome/Android; other browsers fall back to
// typing the number) and fills the product name from data/barcodes.yaml. An
// unknown barcode asks for the name once and is remembered server-side, so
// the next scan of the same package fills instantly.
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

  async function scanCamera() {
    if (!("BarcodeDetector" in window)) {
      return window.prompt("Scanner niet beschikbaar op dit apparaat; typ de EAN (alleen cijfers):");
    }
    const stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "environment" },
    });
    const video = document.createElement("video");
    video.setAttribute("playsinline", "");
    video.style.display = "none";
    video.srcObject = stream;
    document.body.appendChild(video);
    await video.play();
    const detector = new BarcodeDetector();
    try {
      for (;;) {
        const codes = await detector.detect(video);
        if (codes.length) return codes[0].rawValue;
        await new Promise((resolve) => setTimeout(resolve, 250));
      }
    } finally {
      stream.getTracks().forEach((track) => track.stop());
      video.remove();
    }
  }

  for (const button of document.querySelectorAll("[data-barcode-scan]")) {
    button.addEventListener("click", async () => {
      const input = document.getElementById(button.dataset.barcodeScan);
      if (!input) return;
      try {
        const ean = (await scanCamera() || "").trim();
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
})();
