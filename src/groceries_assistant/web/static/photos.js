/* Photo picking: drag & drop and the camera button feed the page's real file
   input, so the form posts exactly like a hand-picked selection. Without JS
   the ordinary "Kies foto's" picker still works. */
(function () {
  var IMAGE_RE = /\.(jpe?g|png|webp)$/i;

  function label(file) {
    return file.name || 'foto';
  }

  function render(box, input) {
    var list = box.querySelector('.file-list');
    if (!list) {
      return;
    }
    var files = input.files ? Array.prototype.slice.call(input.files) : [];
    list.innerHTML = '';
    files.forEach(function (file) {
      var item = document.createElement('li');
      item.textContent = label(file);
      list.appendChild(item);
    });
  }

  function addFiles(box, input, incoming) {
    var accepted = [];
    var skipped = [];
    Array.prototype.slice.call(incoming).forEach(function (file) {
      (IMAGE_RE.test(file.name) ? accepted : skipped).push(file);
    });
    if (accepted.length) {
      var transfer = new DataTransfer();
      Array.prototype.slice.call(input.files || []).forEach(function (file) {
        transfer.items.add(file);
      });
      accepted.forEach(function (file) {
        transfer.items.add(file);
      });
      input.files = transfer.files;
      input.dispatchEvent(new Event('change', { bubbles: true }));
    }
    var warn = box.querySelector('.file-warn');
    if (warn) {
      warn.textContent = skipped.length
        ? 'Genegeerd, alleen jpg/png/webp: ' + skipped.map(label).join(', ')
        : '';
    }
    render(box, input);
  }

  document.querySelectorAll('.photo-drop').forEach(function (box) {
    var input = box.querySelector('input[type="file"][name]');
    if (!input) {
      return;
    }
    input.addEventListener('change', function () {
      render(box, input);
    });

    var camera = box.querySelector('.camera-input');
    var button = box.querySelector('.camera-button');
    if (camera && button) {
      button.addEventListener('click', function () {
        camera.click();
      });
      camera.addEventListener('change', function () {
        addFiles(box, input, camera.files);
        camera.value = ''; // allow taking the same shot twice
      });
    }

    ['dragenter', 'dragover'].forEach(function (name) {
      box.addEventListener(name, function (event) {
        event.preventDefault();
        box.classList.add('dragging');
      });
    });
    box.addEventListener('dragleave', function (event) {
      if (event.relatedTarget && box.contains(event.relatedTarget)) {
        return; // moving onto a child, not out of the box
      }
      box.classList.remove('dragging');
    });
    box.addEventListener('drop', function (event) {
      event.preventDefault();
      box.classList.remove('dragging');
      if (event.dataTransfer && event.dataTransfer.files) {
        addFiles(box, input, event.dataTransfer.files);
      }
    });
  });
})();
