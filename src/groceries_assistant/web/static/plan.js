// Drag & drop re-ordering for the week table: grab the handle, drop on
// another day, and the evenings in between shift by one (POST /plan/move-day
// and a full reload). HTML5 drag is mouse-only; touch and keyboard keep the
// existing 'ruil dag' forms as their fallback.
(function () {
  const table = document.querySelector("table.plan");
  const form = document.getElementById("move-day-form");
  if (!table || !form) return;
  let dragDay = null;

  const clearMarks = () =>
    table.querySelectorAll(".drop-target").forEach((row) => row.classList.remove("drop-target"));

  for (const handle of table.querySelectorAll(".drag-handle")) {
    handle.addEventListener("dragstart", (event) => {
      dragDay = handle.closest("tr").dataset.day;
      event.dataTransfer.setData("text/plain", dragDay);
      event.dataTransfer.effectAllowed = "move";
    });
    handle.addEventListener("dragend", () => {
      dragDay = null;
      clearMarks();
    });
  }

  for (const row of table.querySelectorAll("tbody tr")) {
    row.addEventListener("dragover", (event) => {
      if (dragDay && row.dataset.day && row.dataset.day !== dragDay) {
        event.preventDefault();
        row.classList.add("drop-target");
      }
    });
    row.addEventListener("dragleave", () => row.classList.remove("drop-target"));
    row.addEventListener("drop", (event) => {
      event.preventDefault();
      const to = row.dataset.day;
      if (dragDay && to && to !== dragDay) {
        form.day.value = dragDay;
        form.to.value = to;
        form.submit();
      }
      dragDay = null;
      clearMarks();
    });
  }
})();
