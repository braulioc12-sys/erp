"""Clasificación de año/mes para las constancias de pago (18 sep, 4ta
ronda — pedido de Braulio: "quiero que el menu de Pagos personal este
agrupado por año y luego mes, y una vez que se entra a cada mes pueda ver
pdfs de constancias de pago antiguas ... quiero poder subir las
constancias que me brindara cada banco", seguido de un ZIP real con años
de archivos ya organizados en carpetas "archivos telecredito/2025/junio
2025/..." para hacer la carga inicial masiva).

Este módulo solo se encarga de, dado el path de un archivo DENTRO del zip
que suba Braulio, adivinar a qué periodo (año, mes) pertenece — la carga
real (guardar el archivo con app/storage.py e insertar en la tabla
payment_vouchers) vive en app/routes/pagos_personal.py
(constancias_import_zip()).

Se probó contra el ZIP real que mandó Braulio (314 archivos): de 336
entradas (incluye carpetas), clasificó correctamente 313 de 314 archivos
sin ninguna corrección a mano, dejando solo 1 sin poder determinar el año
(un archivo suelto "liquidados enero.pdf" sin carpeta ni año en el
nombre) — ese y cualquier otro que no se pueda clasificar se reportan
como "no importados" en vez de adivinar mal, para que se suban a mano
desde el mes que corresponda.

Reglas, en este orden:

1. Si en el camino del archivo dentro del zip hay una carpeta con 4
   dígitos que empiece con "20" (ej. "2025"), ese es el año. Si justo
   después de esa carpeta hay otra carpeta (el mes, ej. "junio 2025"),
   el mes sale de buscar el nombre de un mes en español dentro de ese
   nombre de carpeta (ignorando tildes/mayúsculas, y tolerando espacios
   de más — ej. "octubre  2025" con doble espacio).
2. Si la carpeta de año no tiene subcarpeta de mes (el archivo cuelga
   directo de "2025/archivo.pdf"), el mes se busca en el nombre del
   propio archivo.
3. Si el archivo no está dentro de ninguna carpeta de año (suelto en la
   raíz del zip), se busca una abreviatura de mes pegada a 2 dígitos de
   año en el nombre del archivo (ej. "NOV24", "ENERO25") o, si no,
   el nombre completo del mes en cualquier parte + un número de 2 dígitos
   suelto en el nombre como año (ej. "gratiDICIEMBRECUMBAZA24" ->
   diciembre, año 24).
4. Si no se pudo determinar año Y mes con ninguna regla, se devuelve
   (None, None) — ese archivo no se importa solo."""
import re
import unicodedata

MONTH_LABELS = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril", 5: "Mayo", 6: "Junio",
    7: "Julio", 8: "Agosto", 9: "Setiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre",
}

_MONTHS_FULL = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "setiembre": 9, "septiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}
_MONTHS_ABBR = {
    "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "sep": 9, "oct": 10, "nov": 11, "dic": 12,
}
_YEAR_FOLDER_RE = re.compile(r"^20\d{2}$")


def _strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _find_month_full(text):
    """Busca el nombre completo de un mes dentro de `text`. Devuelve
    (numero_de_mes, palabra_encontrada) o (None, None)."""
    t = _strip_accents(text.lower())
    for name, num in _MONTHS_FULL.items():
        if name in t:
            return num, name
    return None, None


def _find_year_loose(text):
    """Primer número de 2 dígitos suelto (no seguido de otro dígito) en el
    texto, interpretado como año 20XX."""
    t = _strip_accents(text.lower())
    m = re.search(r"(\d{2})(?!\d)", t)
    return 2000 + int(m.group(1)) if m else None


def _find_month_abbr_with_year(text):
    """Busca una abreviatura de mes pegada a 2 dígitos de año (ej.
    "nov24", "ene25"). Devuelve (mes, año) o (None, None)."""
    t = _strip_accents(text.lower())
    for name, num in _MONTHS_ABBR.items():
        m = re.search(name + r"(\d{2})", t)
        if m:
            return num, 2000 + int(m.group(1))
    return None, None


def classify_zip_entry(path):
    """path: el nombre completo de una entrada del zip (ej. "archivos
    telecredito/2025/junio 2025/brms junio 25.pdf"), tal cual lo da
    zipfile.ZipFile.namelist(). Devuelve (year, month) o (None, None) si
    no se pudo determinar con confianza."""
    segments = [s for s in path.split("/") if s]
    if not segments:
        return None, None
    filename = segments[-1]
    dirs = segments[:-1]

    year = None
    year_idx = None
    for i, seg in enumerate(dirs):
        if _YEAR_FOLDER_RE.match(seg.strip()):
            year = int(seg.strip())
            year_idx = i
            break

    if year is not None:
        month = None
        if year_idx + 1 < len(dirs):
            month, _ = _find_month_full(dirs[year_idx + 1])
        if month is None:
            month, _ = _find_month_full(filename)
        return (year, month) if month else (None, None)

    # Sin carpeta de año: archivo suelto, todo sale del nombre de archivo.
    month, year = _find_month_abbr_with_year(filename)
    if month:
        return year, month
    month, word = _find_month_full(filename)
    if month:
        year = _find_year_loose(filename)
        if year:
            return year, month
    return None, None
