/* 10 sep, pedido de Braulio: "hay manera de a la hora subir documentos y se
 * tome la foto del celular, sea como un scanner?" — al elegir/tomar una
 * foto en cualquier input marcado con la clase "js-scan-input", se abre un
 * recorte semi-automático: la persona ajusta con el dedo las 4 esquinas
 * del documento (ya vienen puestas cerca del borde de la foto), y al
 * confirmar se endereza (perspectiva) y se mejora el contraste — sin
 * depender de ninguna librería externa ni de que el navegador tenga un
 * "modo escáner" nativo (eso no se puede invocar desde una página web,
 * solo lo tienen apps nativas).
 *
 * Funciona sobre el MISMO <input type="file">: al confirmar, se reemplaza
 * el archivo elegido por la versión ya recortada/mejorada (vía
 * DataTransfer), así el resto del formulario (CSRF, multipart, las rutas
 * del servidor) sigue funcionando exactamente igual que antes, sin ningún
 * cambio del lado del servidor.
 *
 * Si la foto no se puede previsualizar (por ejemplo un .heic en un
 * navegador que no lo sabe decodificar — pasa en algunos Android/Chrome,
 * no en iPhone/Safari) simplemente no se abre el recorte y se sube la
 * foto tal cual, como ya funcionaba antes de este cambio. Los PDF nunca
 * pasan por acá.
 */
(function () {
  "use strict";

  var GRID = 12; // subdivisiones de la cuadrícula usada para "enderezar" la foto
  var MAX_WORKING_DIMENSION = 1600; // mismo criterio que RECEIPT_MAX_DIMENSION/PHOTO_MAX_DIMENSION del servidor
  var MAX_OUTPUT_DIMENSION = 1600;
  var JPEG_QUALITY = 0.85;

  var overlay, imgWrap, previewImg, svgQuad, handles, bwCheckbox, useBtn, skipBtn, resetBtn;
  var activeInput = null;
  var objectUrl = null;
  var naturalW = 0, naturalH = 0, displayW = 0, displayH = 0;
  var corners = [];
  var defaultCorners = [];

  function isProbablyImage(file) {
    if (!file) return false;
    if (file.type && file.type.indexOf("image/") === 0) return true;
    // Algunos navegadores no rellenan bien `type` para HEIC/HEIF — nos fijamos en la extensión.
    return /\.(png|jpe?g|webp|heic|heif|gif|bmp)$/i.test(file.name || "");
  }

  function clampByte(v) {
    return v < 0 ? 0 : v > 255 ? 255 : v;
  }

  function dist(p, q) {
    var dx = p.x - q.x, dy = p.y - q.y;
    return Math.sqrt(dx * dx + dy * dy);
  }

  function clamp(v, lo, hi) {
    return v < lo ? lo : v > hi ? hi : v;
  }

  // --- Construcción del modal (una sola vez, reusado por todos los inputs de la página) ---

  function buildOverlay() {
    if (overlay) return;
    overlay = document.createElement("div");
    overlay.className = "doc-scanner-overlay";
    overlay.innerHTML =
      '<div class="doc-scanner-box">' +
      '<p class="doc-scanner-title">Ajusta las 4 esquinas del documento y confirma</p>' +
      '<div class="doc-scanner-imgwrap">' +
      '<img class="doc-scanner-img" alt="">' +
      '<svg class="doc-scanner-svg"><polygon class="doc-scanner-quad" points=""></polygon></svg>' +
      '<div class="doc-scanner-handle" data-idx="0"></div>' +
      '<div class="doc-scanner-handle" data-idx="1"></div>' +
      '<div class="doc-scanner-handle" data-idx="2"></div>' +
      '<div class="doc-scanner-handle" data-idx="3"></div>' +
      "</div>" +
      '<label class="doc-scanner-bw"><input type="checkbox" checked> Blanco y negro (más legible)</label>' +
      '<div class="doc-scanner-actions">' +
      '<button type="button" class="btn btn-secondary btn-sm doc-scanner-reset">Reiniciar esquinas</button>' +
      '<button type="button" class="btn btn-secondary btn-sm doc-scanner-skip">Usar foto original</button>' +
      '<button type="button" class="btn btn-primary btn-sm doc-scanner-use">Usar foto recortada</button>' +
      "</div>" +
      "</div>";
    document.body.appendChild(overlay);

    imgWrap = overlay.querySelector(".doc-scanner-imgwrap");
    previewImg = overlay.querySelector(".doc-scanner-img");
    svgQuad = overlay.querySelector(".doc-scanner-quad");
    handles = Array.prototype.slice.call(overlay.querySelectorAll(".doc-scanner-handle"));
    bwCheckbox = overlay.querySelector(".doc-scanner-bw input");
    useBtn = overlay.querySelector(".doc-scanner-use");
    skipBtn = overlay.querySelector(".doc-scanner-skip");
    resetBtn = overlay.querySelector(".doc-scanner-reset");

    handles.forEach(function (h, idx) {
      var dragging = false;
      h.addEventListener("pointerdown", function (e) {
        dragging = true;
        h.setPointerCapture(e.pointerId);
        e.preventDefault();
      });
      h.addEventListener("pointermove", function (e) {
        if (!dragging) return;
        var rect = imgWrap.getBoundingClientRect();
        corners[idx] = {
          x: clamp(e.clientX - rect.left, 0, displayW),
          y: clamp(e.clientY - rect.top, 0, displayH),
        };
        layoutHandles();
        e.preventDefault();
      });
      function endDrag() {
        dragging = false;
      }
      h.addEventListener("pointerup", endDrag);
      h.addEventListener("pointercancel", endDrag);
    });

    resetBtn.addEventListener("click", function () {
      corners = defaultCorners.map(function (c) {
        return { x: c.x, y: c.y };
      });
      layoutHandles();
    });
    skipBtn.addEventListener("click", closeOverlay);
    useBtn.addEventListener("click", confirmScan);
    overlay.addEventListener("click", function (e) {
      if (e.target === overlay) closeOverlay();
    });
  }

  function layoutHandles() {
    handles.forEach(function (h, idx) {
      var c = corners[idx];
      h.style.left = c.x + "px";
      h.style.top = c.y + "px";
    });
    svgQuad.setAttribute(
      "points",
      corners.map(function (c) { return c.x + "," + c.y; }).join(" ")
    );
  }

  // --- Abrir / cerrar ---

  function cleanupObjectUrl() {
    if (objectUrl) {
      URL.revokeObjectURL(objectUrl);
      objectUrl = null;
    }
  }

  function closeOverlay() {
    overlay.classList.remove("open");
    document.body.classList.remove("doc-scanner-lock");
    cleanupObjectUrl();
    activeInput = null;
  }

  function openScanner(input, file) {
    buildOverlay();
    activeInput = input;
    cleanupObjectUrl();
    objectUrl = URL.createObjectURL(file);

    // El modal se muestra DE UNA (antes de que la imagen termine de
    // cargar) — si se mide el tamaño mostrado de la imagen mientras el
    // overlay todavía está en display:none, el navegador siempre da 0x0
    // (un elemento dentro de algo no-renderizado no tiene layout). Al
    // volver a elegir una foto se reinicia el tamaño del wrapper para no
    // arrastrar las medidas de la foto anterior mientras carga la nueva.
    imgWrap.style.width = "";
    imgWrap.style.height = "";
    overlay.classList.add("open");
    document.body.classList.add("doc-scanner-lock");

    previewImg.onload = function () {
      naturalW = previewImg.naturalWidth;
      naturalH = previewImg.naturalHeight;
      // Se espera un frame a que el navegador ya haya aplicado el
      // max-width/max-height del CSS antes de medir el tamaño mostrado.
      requestAnimationFrame(function () {
        var rect = previewImg.getBoundingClientRect();
        displayW = rect.width;
        displayH = rect.height;
        imgWrap.style.width = displayW + "px";
        imgWrap.style.height = displayH + "px";
        svgQuad.parentElement.setAttribute("viewBox", "0 0 " + displayW + " " + displayH);

        defaultCorners = [
          { x: displayW * 0.06, y: displayH * 0.06 },
          { x: displayW * 0.94, y: displayH * 0.06 },
          { x: displayW * 0.94, y: displayH * 0.94 },
          { x: displayW * 0.06, y: displayH * 0.94 },
        ];
        corners = defaultCorners.map(function (c) {
          return { x: c.x, y: c.y };
        });
        layoutHandles();
      });
    };
    previewImg.onerror = function () {
      // No se pudo decodificar la foto en este navegador (p. ej. .heic en
      // Chrome/Android) — se cierra el modal y se deja la foto original tal
      // cual, sin recorte.
      closeOverlay();
    };
    previewImg.src = objectUrl;
  }

  // --- El recorte/enderezado en sí (cuadrícula de triángulos, sin librerías) ---

  function bilerp(quad, u, v) {
    // quad = [TL, TR, BR, BL]
    var top = {
      x: quad[0].x + (quad[1].x - quad[0].x) * u,
      y: quad[0].y + (quad[1].y - quad[0].y) * u,
    };
    var bottom = {
      x: quad[3].x + (quad[2].x - quad[3].x) * u,
      y: quad[3].y + (quad[2].y - quad[3].y) * u,
    };
    return { x: top.x + (bottom.x - top.x) * v, y: top.y + (bottom.y - top.y) * v };
  }

  function affineFromTriangle(s0, s1, s2, d0, d1, d2) {
    var denom = s0.x * (s1.y - s2.y) + s1.x * (s2.y - s0.y) + s2.x * (s0.y - s1.y);
    if (!denom) return null;
    var a = (d0.x * (s1.y - s2.y) + d1.x * (s2.y - s0.y) + d2.x * (s0.y - s1.y)) / denom;
    var b = (d0.y * (s1.y - s2.y) + d1.y * (s2.y - s0.y) + d2.y * (s0.y - s1.y)) / denom;
    var c = (d0.x * (s2.x - s1.x) + d1.x * (s0.x - s2.x) + d2.x * (s1.x - s0.x)) / denom;
    var d = (d0.y * (s2.x - s1.x) + d1.y * (s0.x - s2.x) + d2.y * (s1.x - s0.x)) / denom;
    var e =
      (d0.x * (s1.x * s2.y - s2.x * s1.y) +
        d1.x * (s2.x * s0.y - s0.x * s2.y) +
        d2.x * (s0.x * s1.y - s1.x * s0.y)) /
      denom;
    var f =
      (d0.y * (s1.x * s2.y - s2.x * s1.y) +
        d1.y * (s2.x * s0.y - s0.x * s2.y) +
        d2.y * (s0.x * s1.y - s1.x * s0.y)) /
      denom;
    return [a, b, c, d, e, f];
  }

  // Cada celda de la cuadrícula se pinta como 2 triángulos por separado
  // (clip + transform + drawImage), y el clip de canvas siempre sale con
  // los bordes suavizados (antialias) — sin esto, el borde de un triángulo
  // y el borde del vecino no coinciden pixel a pixel, y esa rendija de
  // medio pixel se ve como una línea (el canvas rellena lo "vacío" con
  // negro al exportar a JPEG, que no tiene canal alfa). La solución es
  // agrandar levemente el triángulo de recorte (alejando cada vértice de
  // su centro un par de píxeles) para que los triángulos vecinos se
  // superpongan un poco en vez de dejar una rendija — la transformación
  // afín sigue siendo la calculada con los puntos originales (sin agrandar),
  // así que el "de más" que se pinta es una extrapolación del mismo
  // contenido del triángulo, no basura.
  function expandTriangle(p0, p1, p2, amount) {
    var cx = (p0.x + p1.x + p2.x) / 3;
    var cy = (p0.y + p1.y + p2.y) / 3;
    function push(p) {
      var dx = p.x - cx, dy = p.y - cy;
      var len = Math.sqrt(dx * dx + dy * dy) || 1;
      return { x: p.x + (dx / len) * amount, y: p.y + (dy / len) * amount };
    }
    return [push(p0), push(p1), push(p2)];
  }

  function drawWarpedTriangle(srcCanvas, outCtx, s0, s1, s2, d0, d1, d2) {
    var m = affineFromTriangle(s0, s1, s2, d0, d1, d2);
    if (!m) return;
    var clipPts = expandTriangle(d0, d1, d2, 0.75);
    outCtx.save();
    outCtx.beginPath();
    outCtx.moveTo(clipPts[0].x, clipPts[0].y);
    outCtx.lineTo(clipPts[1].x, clipPts[1].y);
    outCtx.lineTo(clipPts[2].x, clipPts[2].y);
    outCtx.closePath();
    outCtx.clip();
    outCtx.setTransform(m[0], m[1], m[2], m[3], m[4], m[5]);
    outCtx.drawImage(srcCanvas, 0, 0);
    outCtx.restore();
  }

  function warpQuadToRect(srcCanvas, srcCorners, outCtx, outW, outH) {
    var cellW = outW / GRID;
    var cellH = outH / GRID;
    for (var j = 0; j < GRID; j++) {
      for (var i = 0; i < GRID; i++) {
        var u0 = i / GRID, u1 = (i + 1) / GRID, v0 = j / GRID, v1 = (j + 1) / GRID;
        var s00 = bilerp(srcCorners, u0, v0);
        var s10 = bilerp(srcCorners, u1, v0);
        var s01 = bilerp(srcCorners, u0, v1);
        var s11 = bilerp(srcCorners, u1, v1);
        var d00 = { x: i * cellW, y: j * cellH };
        var d10 = { x: (i + 1) * cellW, y: j * cellH };
        var d01 = { x: i * cellW, y: (j + 1) * cellH };
        var d11 = { x: (i + 1) * cellW, y: (j + 1) * cellH };
        drawWarpedTriangle(srcCanvas, outCtx, s00, s10, s01, d00, d10, d01);
        drawWarpedTriangle(srcCanvas, outCtx, s10, s11, s01, d10, d11, d01);
      }
    }
  }

  // Mejora tipo "escáner": estira el contraste (usando el mismo estiramiento
  // en los 3 canales, calculado sobre la luminancia, para no generar un
  // tinte de color raro) y, si se pide, pasa todo a blanco y negro.
  function enhance(ctx, w, h, bw) {
    var imgData = ctx.getImageData(0, 0, w, h);
    var data = imgData.data;
    var i, lum, min = 255, max = 0;
    var n = data.length;
    for (i = 0; i < n; i += 4) {
      lum = 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2];
      if (lum < min) min = lum;
      if (lum > max) max = lum;
    }
    var range = max - min;
    if (range < 10) {
      min = 0;
      range = 255; // foto ya muy plana: no forzar un contraste absurdo
    }
    var scale = 255 / range;
    for (i = 0; i < n; i += 4) {
      if (bw) {
        lum = 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2];
        var v = clampByte((lum - min) * scale);
        data[i] = data[i + 1] = data[i + 2] = v;
      } else {
        data[i] = clampByte((data[i] - min) * scale);
        data[i + 1] = clampByte((data[i + 1] - min) * scale);
        data[i + 2] = clampByte((data[i + 2] - min) * scale);
      }
    }
    ctx.putImageData(imgData, 0, 0);
  }

  function dataURLToBlob(dataURL) {
    var parts = dataURL.split(",");
    var mimeMatch = parts[0].match(/:(.*?);/);
    var mime = mimeMatch ? mimeMatch[1] : "image/jpeg";
    var binary = atob(parts[1]);
    var len = binary.length;
    var bytes = new Uint8Array(len);
    for (var k = 0; k < len; k++) bytes[k] = binary.charCodeAt(k);
    return new Blob([bytes], { type: mime });
  }

  function buildScannedBlob(bw) {
    var workingScale = Math.min(1, MAX_WORKING_DIMENSION / Math.max(naturalW, naturalH));
    var workingW = Math.max(1, Math.round(naturalW * workingScale));
    var workingH = Math.max(1, Math.round(naturalH * workingScale));

    var srcCanvas = document.createElement("canvas");
    srcCanvas.width = workingW;
    srcCanvas.height = workingH;
    srcCanvas.getContext("2d").drawImage(previewImg, 0, 0, workingW, workingH);

    // De coordenadas de pantalla (sobre la imagen ya escalada para caber en
    // el modal) a coordenadas del canvas de trabajo.
    var toWorking = workingW / displayW;
    var srcCorners = corners.map(function (c) {
      return { x: c.x * toWorking, y: c.y * toWorking };
    });

    var topW = dist(srcCorners[0], srcCorners[1]);
    var bottomW = dist(srcCorners[3], srcCorners[2]);
    var leftH = dist(srcCorners[0], srcCorners[3]);
    var rightH = dist(srcCorners[1], srcCorners[2]);
    var outW = Math.max(80, Math.round((topW + bottomW) / 2));
    var outH = Math.max(80, Math.round((leftH + rightH) / 2));
    var outScale = Math.min(1, MAX_OUTPUT_DIMENSION / Math.max(outW, outH));
    outW = Math.max(1, Math.round(outW * outScale));
    outH = Math.max(1, Math.round(outH * outScale));

    var outCanvas = document.createElement("canvas");
    outCanvas.width = outW;
    outCanvas.height = outH;
    var outCtx = outCanvas.getContext("2d");
    outCtx.imageSmoothingEnabled = true;
    if ("imageSmoothingQuality" in outCtx) outCtx.imageSmoothingQuality = "high";
    // Fondo blanco de respaldo: si quedara alguna rendija entre triángulos
    // pese al "sangrado" de expandTriangle(), que se note como blanco
    // (casi invisible en un documento) y no como negro.
    outCtx.fillStyle = "#fff";
    outCtx.fillRect(0, 0, outW, outH);

    warpQuadToRect(srcCanvas, srcCorners, outCtx, outW, outH);
    enhance(outCtx, outW, outH, bw);

    return dataURLToBlob(outCanvas.toDataURL("image/jpeg", JPEG_QUALITY));
  }

  function scannedFileName(originalName) {
    var base = (originalName || "foto").replace(/\.[a-z0-9]+$/i, "");
    return base + "-escaneado.jpg";
  }

  function confirmScan() {
    if (!activeInput) {
      closeOverlay();
      return;
    }
    var input = activeInput;
    var bw = bwCheckbox.checked;
    useBtn.disabled = true;
    skipBtn.disabled = true;
    useBtn.textContent = "Procesando…";
    // Se deja pasar un frame para que el navegador pinte el estado
    // "Procesando…" antes de hacer el trabajo pesado (que congela el hilo).
    setTimeout(function () {
      try {
        var blob = buildScannedBlob(bw);
        var newFile = new File([blob], scannedFileName(input._docScannerOriginalName), { type: "image/jpeg" });
        var dt = new DataTransfer();
        dt.items.add(newFile);
        input.files = dt.files;
      } catch (err) {
        // Si algo falla en el recorte/mejora no se pierde la foto: el input
        // se queda con la foto original ya elegida (no se toca), y el
        // formulario sigue su flujo normal.
        if (window.console && console.warn) {
          console.warn("doc-scanner: no se pudo procesar la foto, se sube la original.", err);
        }
      }
      useBtn.disabled = false;
      skipBtn.disabled = false;
      useBtn.textContent = "Usar foto recortada";
      closeOverlay();
    }, 30);
  }

  // --- Enganche a los inputs marcados en cada formulario ---

  function attach(input) {
    input.addEventListener("change", function () {
      var file = input.files && input.files[0];
      if (!file || !isProbablyImage(file)) return; // PDFs y similares pasan sin tocar
      input._docScannerOriginalName = file.name;
      openScanner(input, file);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var inputs = document.querySelectorAll('input[type="file"].js-scan-input');
    Array.prototype.forEach.call(inputs, attach);
  });
})();
