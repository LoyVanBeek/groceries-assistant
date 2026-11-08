// Drag & drop re-ordering for the week table, built on pointer events so the
// mouse and a finger share one code path (HTML5 drag events never fire on
// touch; the handle therefore has no draggable attribute and carries
// touch-action: none). A drag starts on the ⋮⋮ handle: the row moves live to
// show where it will land, a ghost card rides the cursor, and releasing
// commits the move with a fetch — the row is already in its previewed slot,
// so a successful drop sticks without a page reload. Listeners live on
// `document` during the drag, so no browser quirk around pointer capture can
// interrupt them. Escape, pointercancel, or dropping outside the table
// restores the original order. Touch without the handle (and the 'ruil dag'
// forms) keeps scrolling and reordering as before.
(function () {
  const table = document.querySelector("table.plan");
  if (!table) return;
  const tbody = table.tBodies[0];

  let dragRow = null;
  let hoverDay = null;
  let ghost = null;
  let originalRows = null;
  let grabbedOffsetX = 0;
  const originalIndex = new Map(); // row -> its slot at dragstart

  function restoreOrder() {
    if (!originalRows) return;
    for (const row of originalRows) tbody.appendChild(row); // re-append in order
    originalRows = null;
  }

  function cleanupVisuals() {
    if (ghost) {
      ghost.remove();
      ghost = null;
    }
    table.querySelectorAll(".dragging").forEach((row) => row.classList.remove("dragging"));
    clearMarks();
  }

  function clearMarks() {
    table.querySelectorAll(".drop-target").forEach((row) => row.classList.remove("drop-target"));
  }

  function dayLabel(row) {
    return (row.querySelector(".col-day .day-abbr") || {}).textContent || "";
  }

  function dishLabel(row) {
    const label = row.querySelector(".col-dish strong, .col-dish .dish-label");
    return label ? label.textContent.trim() : "";
  }

  function makeGhost(row, x, y) {
    ghost = document.createElement("div");
    ghost.className = "drag-ghost";
    const day = document.createElement("strong");
    day.textContent = dayLabel(row);
    const dish = document.createElement("span");
    dish.textContent = " " + dishLabel(row);
    ghost.append(day, dish);
    document.body.appendChild(ghost);
    moveGhost(x, y);
  }

  function moveGhost(x, y) {
    if (!ghost) return;
    ghost.style.transform = "translate(" + (x + grabbedOffsetX) + "px, " + (y - 24) + "px)";
  }

  function beginDrag(row, x, y) {
    dragRow = row;
    hoverDay = null;
    originalRows = [...tbody.children];
    originalRows.forEach((r, i) => originalIndex.set(r, i));
    row.classList.add("dragging");
    makeGhost(row, x, y);
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
    const target = rowUnderPoint(y);
    if (!target) return;
    hoverDay = target.dataset.day;
    // live preview: dragging down, the row takes the hovered slot (after it);
    // dragging up, it lands before it. Direction comes from the original
    // slots, not the moved-around DOM.
    const down = originalIndex.get(dragRow) < originalIndex.get(target);
    const changed =
      down ? dragRow.previousElementSibling !== target : dragRow.nextElementSibling !== target;
    if (changed) {
      if (down) target.after(dragRow);
      else target.before(dragRow);
    }
    clearMarks();
    target.classList.add("drop-target");
  }

  function commit() {
    const day = dragRow.dataset.day;
    // de preview IS de nieuwe stand: de snapshot mag niet meer terughalen
    originalRows = null;
    cleanupVisuals();
    const body = new URLSearchParams({ day: day, to: hoverDay, back: "/plan" });
    fetch("/plan/move-day", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body,
    })
      .then((response) => {
        if (response.url.indexOf("error=") !== -1) {
          // geweigerd (goedgekeurde/bestelde dag in het traject): toon de
          // servermelding door naar de doelpagina te gaan
          window.location.href = response.url;
        }
        // gelukt: de preview is de echte volgorde; niets doen
      })
      .catch(() => {
        // netwerkfout: herlaad om de serverstaat te zien
        window.location.reload();
      });
  }

  function cancelDrag() {
    restoreOrder();
    cleanupVisuals();
  }

  let cleanupListeners = function () {};

  for (const handle of table.querySelectorAll(".drag-handle")) {
    handle.addEventListener("pointerdown", (event) => {
      if (event.pointerType === "mouse" && event.button !== 0) return;
      event.preventDefault();
      const row = handle.closest("tr");
      const startX = event.clientX;
      const startY = event.clientY;
      let dragging = false;

      const onMove = (moveEvent) => {
        if (!dragging) {
          // a small threshold: a tap stays a tap, a move becomes a drag
          if (Math.hypot(moveEvent.clientX - startX, moveEvent.clientY - startY) < 8) return;
          dragging = true;
          grabbedOffsetX = Math.min(startX - row.getBoundingClientRect().left, 40);
          beginDrag(row, moveEvent.clientX, moveEvent.clientY);
        }
        moveDrag(moveEvent.clientX, moveEvent.clientY);
        moveEvent.preventDefault();
      };
      const onUp = (upEvent) => {
        cleanupListeners();
        if (!dragging) {
          dragRow = null;
          return; // a tap on the handle: nothing to do
        }
        const target = rowUnderPoint(upEvent.clientY);
        const to = target ? target.dataset.day : null;
        hoverDay = to || null;
        commit();
      };
      const onCancel = () => {
        cleanupListeners();
        if (dragging) cancelDrag();
        else dragRow = null;
      };
      const onKey = (keyEvent) => {
        if (keyEvent.key === "Escape") {
          keyEvent.preventDefault();
          cleanupListeners();
          if (dragging) cancelDrag();
          else dragRow = null;
        }
      };

      cleanupListeners = function () {
        document.removeEventListener("pointermove", onMove);
        document.removeEventListener("pointerup", onUp);
        document.removeEventListener("pointercancel", onCancel);
        document.removeEventListener("keydown", onKey);
      };
      document.addEventListener("pointermove", onMove);
      document.addEventListener("pointerup", onUp);
      document.addEventListener("pointercancel", onCancel);
      document.addEventListener("keydown", onKey);
    });
  }
})();
