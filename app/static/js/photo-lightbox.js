/* 6 oct: clic en una foto marcada con data-lightbox="<url>" la abre grande
   (sin recorte) sobre un fondo oscuro. Se cierra con clic, Esc o la X. */
(function () {
  function close() {
    var o = document.querySelector('.lb-overlay');
    if (o) o.remove();
  }
  document.addEventListener('click', function (e) {
    var t = e.target.closest ? e.target.closest('[data-lightbox]') : null;
    if (!t) return;
    e.preventDefault();
    e.stopPropagation();
    close();
    var o = document.createElement('div');
    o.className = 'lb-overlay';
    var img = document.createElement('img');
    img.src = t.getAttribute('data-lightbox');
    img.alt = t.getAttribute('data-lightbox-caption') || '';
    var x = document.createElement('span');
    x.className = 'lb-close';
    x.textContent = '×';
    o.appendChild(x);
    o.appendChild(img);
    var cap = t.getAttribute('data-lightbox-caption');
    if (cap) {
      var c = document.createElement('div');
      c.className = 'lb-caption';
      c.textContent = cap;
      o.appendChild(c);
    }
    o.addEventListener('click', function (ev) { ev.stopPropagation(); close(); });
    document.body.appendChild(o);
  }, true);
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') close(); });
})();
