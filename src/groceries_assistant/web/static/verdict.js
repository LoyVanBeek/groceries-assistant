// Waardering (love/ok/dislike/never) instellen mag de pagina niet herladen:
// de tabel blijft precies waar hij staat en wisselt alleen van inhoud.
(function () {
  'use strict';
  document.addEventListener('change', function (event) {
    var select = event.target;
    if (!select || select.name !== 'verdict' || !select.form) return;
    var form = select.form;
    var table = form.closest ? form.closest('table') : null;
    if (!table || !table.id) return;
    fetch(form.getAttribute('action'), {
      method: 'POST',
      body: new FormData(form),
    })
      .then(function () {
        return fetch(window.location.href);
      })
      .then(function (response) {
        return response.text();
      })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, 'text/html');
        var fresh = doc.getElementById(table.id);
        if (fresh) {
          table.innerHTML = fresh.innerHTML;
        }
        history.replaceState(null, '', window.location.href);
      })
      .catch(function () {
        // geen live-update mogelijk: gewoon de oude weg (hele reload)
        form.submit();
      });
  });
})();
