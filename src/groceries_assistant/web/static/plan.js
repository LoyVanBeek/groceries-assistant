// Drag & drop re-ordering for the week table: grab the ⋮⋮ handle, drag over
// another day — the row moves live to show where it will land — and drop to
// commit (POST /plan/move-day and a reload). The server rotates the evenings
// so the dragged dish takes the target's place and the days in between shift
// by one; the live preview shows exactly that end state. HTML5 drag is
// mouse-only: touch and keyboard keep the existing 'ruil dag' forms.
(function () {
  const table = document.querySelector("table.plan");
  const form = document.getElementById("move-day-form");
  if (!table || !form) return;

  const tbody = table.tBodies[0];
  let dragRow = null; // the row being dragged
  let dropped = false;
  let ghost = null;
  let originalRows = null;
  const originalIndex = new Map(); // row -> its slot at dragstart

  function restoreOrder() {
    if (!originalRows) return;
    for (const row of originalRows) tbody.appendChild(row); // re-append in order
    originalRows = null;
  }

  function dayLabel(row) {
    return (row.querySelector(".col-day strong") || {}).textContent || "";
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
    ghost.style.position = "fixed";
    ghost.style.top = "-1000px";
    document.body.appendChild(ghost);
  }

  function clearMarks() {
    table.querySelectorAll(".drop-target").forEach((row) => row.classList.remove("drop-target"));
  }

  for (const handle of table.querySelectorAll(".drag-handle")) {
    handle.addEventListener("dragstart", (event) => {
      dragRow = handle.closest("tr");
      dropped = false;
      originalRows = [...tbody.children];
      originalRows.forEach((row, index) => originalIndex.set(row, index));
      dragRow.classList.add("dragging");
      makeGhost(dragRow);
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", dragRow.dataset.day);
      if (ghost) event.dataTransfer.setDragImage(ghost, 20, 20);
    });
    handle.addEventListener("dragend", () => {
      if (!dropped) restoreOrder(); // dropped outside the table or with Esc
      if (ghost) {
        ghost.remove();
        ghost = null;
      }
      table.querySelectorAll(".dragging").forEach((row) => row.classList.remove("dragging"));
      clearMarks();
      dragRow = null;
    });
  }

  for (const row of table.querySelectorAll("tbody tr")) {
    row.addEventListener("dragover", (event) => {
      if (!dragRow || row === dragRow || !row.dataset.day) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      row.classList.add("drop-target");
      // live preview: dragging down, the row takes the hovered slot (after
      // it); dragging up, it lands before it. Direction comes from the
      // original slots, not the moved-around DOM.
      const down = originalIndex.get(dragRow) < originalIndex.get(row);
      const changed =
        (down ? dragRow.previousElementSibling !== row : dragRow.nextElementSibling !== row);
      if (changed) {
        if (down) row.after(dragRow);
        else row.before(dragRow);
      }
    });
    row.addEventListener("dragleave", () => row.classList.remove("drop-target"));
    row.addEventListener("drop", (event) => {
      event.preventDefault();
      const to = row.dataset.day;
      if (dragRow && to && to !== dragRow.dataset.day) {
        dropped = true;
        form.day.value = dragRow.dataset.day;
        form.to.value = to;
        form.submit(); // navigates; the server reproduces the previewed order
      }
      dragRow = null;
    });
  }
})();
