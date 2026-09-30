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
real, no una imagen escaneada)."""
import io
import re

import pdfplumber


def extract_pdf_text(raw_bytes):
    """Todo el texto que se pueda extraer del PDF (todas sus páginas,
    aunque un comprobante real siempre es de una sola). Nunca lanza
    excepción: un PDF dañado o sin capa de texto (ej. una foto/escaneo)
    simplemente devuelve "" -- manual_zip() lo reporta como "no se encontró
    el PDF de esta factura" en vez de reventar toda la carga por un solo
    archivo problemático."""
    try:
        with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception:
        return ""


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
