"""30 sep, "Cargar factura ya emitida (SUNAT)" -- carga en zip (ver
manual_zip() en app/routes/facturacion.py): Braulio compartió una captura
de su carpeta de descargas donde el PDF y el XML de una misma factura
tienen nombres de archivo que NO se parecen en nada entre sí (el portal de
SUNAT/su navegador los nombra cada uno a su manera). La primera versión de
la carga en zip calzaba PDF con XML por tener el mismo nombre de archivo
-- no sirve con esos nombres reales, así que ahora se calzan por
CONTENIDO: se extrae el texto del PDF y se busca ahí la serie-número
exacta que ya se leyó del XML (ver parse_invoice_xml en
app/integrations/sunat_xml.py), en vez de depender de cómo se llame el
archivo.

Se usa pdfplumber (no pypdf/PyPDF2) por ser más confiable extrayendo texto
de PDFs reales de comprobantes electrónicos (que casi siempre traen texto
real, no una imagen escaneada).

30 sep, mismo día, error real en producción: un PDF real (no los PDF de
prueba, chiquitos y simples, con los que esto se probó acá) hizo que
pdfplumber/pdfminer tardaran tanto extrayendo el texto que gunicorn mató al
worker por "WORKER TIMEOUT" (30s por defecto) a mitad de esa extracción --
el traceback de Render mostró justo eso, adentro de
pdfminer/pdfdevice.py:render_char. Como ese timeout gunicorn lo fuerza con
una señal (termina en SystemExit, no una Exception de Python normal), el
try/except Exception de más abajo NUNCA lo iba a atrapar -- hacía falta
además: (a) limitar la extracción a la primera página nada más (un
comprobante real siempre es de una sola, así que no hay nada que ganar
leyendo el resto, y si por error se subiera un PDF de muchas páginas ya no
sale caro) y (b) un tope de tiempo propio (signal.alarm, 30 sep) para
cortar la extracción de UN solo PDF si se cuelga, en vez de arriesgar el
worker completo -- ver PROCFILE_TIMEOUT_SECONDS en el comentario del
Procfile para el otro lado de este mismo arreglo (subir el timeout de
gunicorn, para que cargar VARIAS facturas reales en un zip no ande al
límite)."""
import io
import re
import signal

import pdfplumber

# Tope de tiempo para leer UN solo PDF -- ver el comentario grande de
# arriba. Generoso para un comprobante real (una página, texto simple) pero
# corta bastante antes del timeout de gunicorn (ver Procfile) para que
# SIEMPRE quede tiempo de reportar ese archivo como "no se pudo leer" y
# seguir con el resto del zip, en vez de arriesgar que gunicorn mate al
# worker a mitad de camino.
_PDF_EXTRACT_TIMEOUT_SECONDS = 10


class _PdfExtractTimeout(Exception):
    """Interno -- nunca se propaga fuera de extract_pdf_text()."""


def _raise_pdf_extract_timeout(signum, frame):
    raise _PdfExtractTimeout()


def extract_pdf_text(raw_bytes):
    """El texto de la PRIMERA página del PDF nada más (un comprobante real
    siempre es de una sola -- ver el comentario grande arriba). Nunca lanza
    excepción ni se cuelga más de _PDF_EXTRACT_TIMEOUT_SECONDS: un PDF
    dañado, sin capa de texto (ej. una foto/escaneo), con muchísimas páginas
    o que por lo que sea tarde demasiado, simplemente devuelve "" --
    manual_zip() lo reporta como "no se encontró el PDF de esta factura" en
    vez de reventar (o colgar) toda la carga por un solo archivo
    problemático."""
    has_alarm = hasattr(signal, "SIGALRM")  # no existe en Windows -- ahí queda sin este tope (solo en desarrollo)
    old_handler = None
    try:
        if has_alarm:
            old_handler = signal.signal(signal.SIGALRM, _raise_pdf_extract_timeout)
            signal.alarm(_PDF_EXTRACT_TIMEOUT_SECONDS)
        with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
            first_page = pdf.pages[0] if pdf.pages else None
            return (first_page.extract_text() or "") if first_page else ""
    except Exception:
        return ""
    finally:
        if has_alarm:
            signal.alarm(0)
            if old_handler is not None:
                signal.signal(signal.SIGALRM, old_handler)


def pdf_text_matches_invoice(text, series, correlativo):
    """¿El texto del PDF menciona esta serie-número exacta? Tolera que el
    PDF la imprima con ceros a la izquierda distintos a los del XML (ej.
    XML trae correlativo=4882, el PDF puede imprimir "E001-004882" o
    "E001-4882") y espacios alrededor del guión -- pero exige que no le
    sigan más dígitos, para no confundir la factura 4882 con la 48820."""
    if not text or not series or not correlativo:
        return False
    pattern = re.compile(rf"{re.escape(series)}\s*-\s*0*{correlativo}(?!\d)", re.IGNORECASE)
    return bool(pattern.search(text))
