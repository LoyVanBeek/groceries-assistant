// Drag & drop re-ordering for the week table, built on pointer events so the
// mouse and a finger share one code path (HTML5 drag events never fire on
// touch). A drag starts on the ⋮⋮ handle: the row moves live to show where it
// will land, the ghost card follows the finger, and releasing posts
// /plan/move-day and reloads. The handle has touch-action: none, so a drag
// never turns into a page scroll (and pull-to-refresh never fights it).
// Everything else on the page keeps scrolling normally.
(function () {
  const table = document.querySelector("table.plan");
  const form = document.getElementById("move-day-form");
  if (!table || !form) return;
  const tbody = table.tBodies[0];

  let dragRow = null;
  let hoverDay = null;
  let ghost = null;
  let originalRows = null;
  const originalIndex = new Map(); // row -> its slot at dragstart

  function restoreOrder() {
    if (!originalRows) return;
    for (const row of originalRows) tbody.appendChild(row); // re-append in order
    originalRows = null;
  }

  function dayLabel(row) {
    return (row.querySelector(".col-day .day-abbr") || {}).textContent || "";
  }

  function dishLabel(row) {
    const label = row.querySelector(".col-dish strong, .col-dish .dish-label");
    return label ? label.textContent.trim() : "";
  }

  function makeGhost(row) {
    ghost = document.createElement("div");
    ghost.className = "drag-ghost";
    const day = document.createElement("strong");
    day.textContent = dayLabel(row);
    const dish = document.createElement("span");
    dish.textContent = " " + dishLabel(row);
    ghost.append(day, dish);
    document.body.appendChild(ghost);
  }

  function moveGhost(x, y) {
    if (!ghost) return;
    ghost.style.left = x + 14 + "px";
    ghost.style.top = y - 16 + "px";
  }

  function clearMarks() {
    table.querySelectorAll(".drop-target").forEach((row) => row.classList.remove("drop-target"));
  }

  function beginDrag(row) {
    dragRow = row;
    hoverDay = null;
    originalRows = [...tbody.children];
    originalRows.forEach((r, i) => originalIndex.set(r, i));
    row.classList.add("dragging");
    makeGhost(row);
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

  function endDrag(to) {
    if (ghost) {
      ghost.remove();
      ghost = null;
    }
    table.querySelectorAll(".dragging").forEach((row) => row.classList.remove("dragging"));
    clearMarks();
    const day = dragRow ? dragRow.dataset.day : null;
    if (to && day && to !== day) {
      form.day.value = day;
      form.to.value = to;
      form.submit(); // navigates; the server reproduces the previewed order
    } else if (dragRow) {
      restoreOrder(); // cancelled: no move
    }
    dragRow = null;
    hoverDay = null;
    originalRows = null;
  }

  for (const handle of table.querySelectorAll(".drag-handle")) {
    handle.addEventListener("pointerdown", (event) => {
      if (event.pointerType === "mouse" && event.button !== 0) return;
      event.preventDefault();
      handle.setPointerCapture(event.pointerId);
      const startX = event.clientX;
      const startY = event.clientY;
      let dragging = false;

      const onMove = (moveEvent) => {
        if (!dragging) {
          // a small threshold: a tap stays a tap, a move becomes a drag
          if (Math.hypot(moveEvent.clientX - startX, moveEvent.clientY - startY) < 8) return;
          dragging = true;
          beginDrag(handle.closest("tr"));
        }
        moveDrag(moveEvent.clientX, moveEvent.clientY);
        moveEvent.preventDefault();
      };
      const onEnd = (endEvent) => {
        handle.removeEventListener("pointermove", onMove);
        const target = dragging && dragRow ? rowUnderPoint(endEvent.clientY) : null;
        const to = target ? target.dataset.day : null;
        endDrag(to);
      };
      handle.addEventListener("pointermove", onMove);
      handle.addEventListener("pointerup", onEnd, { once: true });
      handle.addEventListener("pointercancel", onEnd, { once: true });
    });
  }
})();
