// Drag & drop on the week table, built on pointer events so the mouse and a
// finger share one code path (HTML5 drag events never fire on touch; the
// handle therefore has no draggable attribute and carries touch-action: none).
// Two drag sources, with the pointerdown handled through event delegation on
// the table (a drop swaps the rows' contents in place, which replaces the
// handle elements — attached listeners would die with them):
//   * the ⋮⋮ handle: a day's DISH swaps with another day's dish. The day grid
//     itself never moves (labels stay in date order); the preview highlights
//     the two rows that will trade places, and the drop exchanges the rows'
//     contents in place and commits with a fetch — no page reload.
//   * the 'restjes' chip: drags one day's leftovers onto a later day, which
//     becomes a restjes evening (server-validated; the page reloads because
//     the evening's kind changes).
//   * the 'vriezer' chip: drags frozen leftovers onto any EMPTY day (no
//     later-only rule — the dish was cooked weeks ago); the server refuses
//     occupied days and the page reloads with the notice.
(function () {
  const table = document.querySelector("table.plan");
  if (!table) return;
  const tbody = table.tBodies[0];
  const zone = document.getElementById("vriezer-zone");

  let dragRow = null; // the row being dragged
  let dragMode = null; // "swap" | "restjes" | "vriezer"
  let sourceDay = null; // the restjes chip's source day
  let sourceSlug = null; // the vriezer chip's dish slug
  let hoverRow = null; // the row the drop would land on
  let hoverZone = false; // the drop would land in the freezer zone
  let ghost = null;
  let grabbedOffsetX = 0;

  function cleanup() {
    if (ghost) {
      ghost.remove();
      ghost = null;
    }
    table.querySelectorAll(".dragging").forEach((row) => row.classList.remove("dragging"));
    clearMarks();
    document.body.style.overflow = "";
    dragRow = null;
    dragMode = null;
    sourceDay = null;
    sourceSlug = null;
    hoverRow = null;
    hoverZone = false;
  }

  function clearMarks() {
    table.querySelectorAll(".drop-target").forEach((row) => row.classList.remove("drop-target"));
    if (zone) zone.classList.remove("drop-target");
  }

  function overZone(x, y) {
    if (!zone) return false;
    const box = zone.getBoundingClientRect();
    return x >= box.left && x <= box.right && y >= box.top && y <= box.bottom;
  }

  function dayLabel(row) {
    return (row.querySelector(".col-day .day-abbr") || {}).textContent || "";
  }

  function dishLabel(row) {
    const label = row.querySelector(".col-dish strong, .col-dish .dish-label");
    return label ? label.textContent.trim() : "";
  }

  function makeGhost(row, x, y, mode, sourceEl) {
    ghost = document.createElement("div");
    ghost.className = "drag-ghost";
    if (mode === "restjes") {
      ghost.textContent = "Restjes van " + dishLabel(row);
    } else if (mode === "vriezer") {
      ghost.textContent = "\u2744 " + sourceEl.textContent.trim();
    } else {
      const day = document.createElement("strong");
      day.textContent = dayLabel(row);
      const dish = document.createElement("span");
      dish.textContent = " " + dishLabel(row);
      ghost.append(day, dish);
    }
    document.body.appendChild(ghost);
    moveGhost(x, y);
  }

  function moveGhost(x, y) {
    if (!ghost) return;
    ghost.style.transform = "translate(" + (x + grabbedOffsetX) + "px, " + (y - 24) + "px)";
  }

  function rowUnderPoint(y) {
    const rows = [...tbody.children].filter((r) => r !== dragRow && r.dataset.day);
    if (!rows.length) return null;
    let target = rows[0];
    for (const row of rows) {
      if (y >= row.getBoundingClientRect().top) target = row;
    }
    return target;
  }

  function moveDrag(x, y) {
    moveGhost(x, y);
    // autorandsrollen: bij de boven-/onderrand scrollt de pagina langzaam mee,
    // zodat een dag onder de vouw bereikbaar blijft zonder de drag te lossen
    const vh = window.innerHeight;
    if (y < 90) window.scrollBy(0, -Math.ceil((90 - y) * 0.2));
    else if (y > vh - 90) window.scrollBy(0, Math.ceil((y - (vh - 90)) * 0.2));
    const zoneHit = overZone(x, y);
    if (zoneHit !== hoverZone) {
      hoverZone = zoneHit;
      clearMarks();
      if (zoneHit) zone.classList.add("drop-target");
    }
    if (zoneHit) {
      if (hoverRow) hoverRow.classList.remove("drop-target");
      hoverRow = null;
      return;
    }
    const target = rowUnderPoint(y);
    if (hoverRow !== target) {
      hoverRow = target;
      clearMarks();
      if (target) target.classList.add("drop-target");
    }
  }

  // de ruil: de inhoud van de twee rijen wisselt (behalve de daglabel-cel)
  function applySwap() {
    const sourceRow = dragRow;
    const targetRow = hoverRow;
    if (!targetRow || targetRow === sourceRow) return;
    const cells = (row) => [...row.children].filter((c) => !c.classList.contains("col-day"));
    const s = cells(sourceRow);
    const t = cells(targetRow);
    for (let i = 0; i < Math.min(s.length, t.length); i++) {
      const tmp = s[i].innerHTML;
      s[i].innerHTML = t[i].innerHTML;
      t[i].innerHTML = tmp;
    }
  }

  function commit() {
    if (hoverZone) {
      // de vriezer-zone: restjes invriezen in plaats van op een dag leggen
      if (dragMode === "restjes" && sourceDay) {
        postForm("/plan/restjes-vriezer", { day: sourceDay });
      } else if (dragMode === "swap" && dragRow && dragRow.dataset.day) {
        if (dragRow.dataset.kind === "leftovers") {
          // de geplande restjes zelf vriezen: de avond wordt leeg
          postForm("/plan/restjes-vriezen", { day: dragRow.dataset.day });
        } else {
          postForm("/plan/restjes-vriezer", { day: dragRow.dataset.day });
        }
      }
      return;
    }
    if (dragMode === "swap") {
      const day = dragRow.dataset.day;
      const swapTo = hoverRow ? hoverRow.dataset.day : "";
      if (!swapTo || swapTo === day) return;
      applySwap(); // de ruil staat al in de DOM; de fetch zet hem in de database
      fetch("/plan/swap-days", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ day: day, swap_to: swapTo }),
      })
        .then((response) => {
          if (response.url.indexOf("error=") !== -1) {
            window.location.href = response.url; // geweigerd: melding + serverstaat
          }
          // gelukt: de DOM-wissel is de nieuwe stand; niets doen
        })
        .catch(() => window.location.reload());
    } else if (dragMode === "vriezer") {
      if (!hoverRow || !sourceSlug) return;
      fetch("/plan/vriezer-eten", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ day: hoverRow.dataset.day, slug: sourceSlug }),
      })
        .then((response) => {
          // de avond verandert van soort: de pagina rendert hem opnieuw
          window.location.href = response.url;
        })
        .catch(() => window.location.reload());
    } else if (dragMode === "restjes") {
      const body = new URLSearchParams({
        source_day: sourceDay,
        day: hoverRow ? hoverRow.dataset.day : "",
      });
      fetch("/plan/restjes", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: body,
      })
        .then((response) => {
          // de avond verandert van soort: de pagina rendert hem opnieuw
          window.location.href = response.url;
        })
        .catch(() => window.location.reload());
    }
  }

  function postForm(url, fields) {
    // alle vriezer-aanroepen veranderen de avond van soort: de pagina rendert
    // opnieuw (de server stuurt de melding mee via de redirect)
    fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams(fields),
    })
      .then((response) => {
        window.location.href = response.url;
      })
      .catch(() => window.location.reload());
  }

  function startDrag(sourceEl, mode, event) {
    dragRow = sourceEl.closest("tr");
    if (!dragRow || !dragRow.dataset.day) return;
    dragMode = mode;
    sourceDay = mode === "restjes" ? sourceEl.dataset.restjesSource : null;
    sourceSlug = mode === "vriezer" ? sourceEl.dataset.vriezerSlug : null;
    hoverRow = null;
    grabbedOffsetX = Math.min(event.clientX - dragRow.getBoundingClientRect().left, 40);
    try {
      sourceEl.setPointerCapture(event.pointerId);
    } catch (err) {
      // zonder capture bereiken de events het element alsnog via bubbelen
    }
    makeGhost(dragRow, event.clientX, event.clientY, mode, sourceEl);
    dragRow.classList.add("dragging");
    // scroll-lock: de pagina scrollt niet onder de vinger/muis; de randen van
    // het scherm scrollen de pagina zelf (moveDrag)
    document.body.style.overflow = "hidden";

    const onMove = (moveEvent) => {
      moveDrag(moveEvent.clientX, moveEvent.clientY);
      moveEvent.preventDefault();
    };
    const onUp = (upEvent) => {
      detach();
      const target = rowUnderPoint(upEvent.clientY);
      hoverRow = target || null;
      commit();
      cleanup();
    };
    const onCancel = () => {
      detach();
      cleanup();
    };
    const onKey = (keyEvent) => {
      if (keyEvent.key === "Escape") {
        keyEvent.preventDefault();
        detach();
        cleanup();
      }
    };
    const detach = () => {
      sourceEl.removeEventListener("pointermove", onMove);
      sourceEl.removeEventListener("pointerup", onUp);
      sourceEl.removeEventListener("pointercancel", onCancel);
      document.removeEventListener("keydown", onKey);
    };
    sourceEl.addEventListener("pointermove", onMove);
    sourceEl.addEventListener("pointerup", onUp, { once: true });
    sourceEl.addEventListener("pointercancel", onCancel, { once: true });
    document.addEventListener("keydown", onKey, { once: true });
  }

  // event-delegatie: een drop vervangt de rijen' inhoud (en dus de handles),
  // dus de listener hangt aan de hele tabel
  table.addEventListener("pointerdown", (event) => {
    const handle = event.target.closest(".drag-handle");
    const chip = event.target.closest(".restjes-chip");
    const vchip = event.target.closest(".vriezer-chip");
    if (!handle && !chip && !vchip) return;
    if (event.pointerType === "mouse" && event.button !== 0) return;
    event.preventDefault();
    if (handle) startDrag(handle, "swap", event);
    else if (chip) startDrag(chip, "restjes", event);
    else if (vchip) startDrag(vchip, "vriezer", event);
  });
})();
