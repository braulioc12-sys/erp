"""Funciones auxiliares compartidas por las rutas."""
import io
from datetime import datetime

from PIL import Image, ImageOps

from app.db import query_all, query_one


def next_code(prefix, table, code_column="code"):
    """Genera un código correlativo tipo V-0001, F-0001, etc."""
    row = query_one(f"SELECT COUNT(*) as n FROM {table}")
    n = (row["n"] if row else 0) + 1
    return f"{prefix}-{n:04d}"


def parse_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_date(value):
    """Valida que la fecha venga en formato YYYY-MM-DD; si no, devuelve None."""
    if not value:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return value
    except ValueError:
        return None


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def now_str():
    """Fecha y hora actual como texto 'YYYY-MM-DD HH:MM:SS' (mismo formato
    que datetime('now') de SQLite), para columnas que necesitan la hora
    exacta y no solo la fecha — ej. trips.actual_start_at/actual_end_at."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def money(value):
    try:
        return f"S/ {float(value):,.2f}"
    except (TypeError, ValueError):
        return "S/ 0.00"


_UNIDADES = ["", "UNO", "DOS", "TRES", "CUATRO", "CINCO", "SEIS", "SIETE", "OCHO", "NUEVE"]
_ESPECIALES_10_19 = ["DIEZ", "ONCE", "DOCE", "TRECE", "CATORCE", "QUINCE", "DIECISEIS",
                     "DIECISIETE", "DIECIOCHO", "DIECINUEVE"]
_DECENAS = ["", "", "VEINTE", "TREINTA", "CUARENTA", "CINCUENTA", "SESENTA", "SETENTA",
            "OCHENTA", "NOVENTA"]
_CENTENAS = ["", "CIENTO", "DOSCIENTOS", "TRESCIENTOS", "CUATROCIENTOS", "QUINIENTOS",
             "SEISCIENTOS", "SETECIENTOS", "OCHOCIENTOS", "NOVECIENTOS"]


def _tres_digitos_a_letras(n):
    """Convierte un número de 0 a 999 a letras en español."""
    if n == 0:
        return ""
    if n == 100:
        return "CIEN"
    resultado = []
    centena, resto = divmod(n, 100)
    if centena:
        resultado.append(_CENTENAS[centena])
    if resto:
        if 10 <= resto <= 19:
            resultado.append(_ESPECIALES_10_19[resto - 10])
        else:
            decena, unidad = divmod(resto, 10)
            if decena == 2 and unidad:
                resultado.append("VEINTI" + _UNIDADES[unidad])
            else:
                partes = [_DECENAS[decena]] if decena else []
                if decena and unidad:
                    partes.append("Y")
                if unidad:
                    partes.append(_UNIDADES[unidad])
                resultado.append(" ".join(p for p in partes if p))
    return " ".join(resultado)


def _apocope_uno(palabras):
    """Cualquier forma terminada en 'UNO' ('UNO', 'VEINTIUNO', 'TREINTA Y
    UNO'...) pierde la O final antes de MIL o MILLONES en español
    ('OCHENTA Y UN MIL', 'VEINTIUN MIL', no '...UNO MIL')."""
    if palabras.endswith("UNO"):
        return palabras[:-1]
    return palabras


def number_to_words_es(n):
    """Convierte un entero no negativo a letras en español (mayúsculas),
    soportando hasta los millones — suficiente para montos de cotizaciones
    y facturas. Ej.: 95580 -> 'NOVENTA Y CINCO MIL QUINIENTOS OCHENTA'."""
    n = int(n)
    if n == 0:
        return "CERO"
    partes = []
    millones, resto = divmod(n, 1_000_000)
    if millones:
        if millones == 1:
            partes.append("UN MILLON")
        else:
            partes.append(f"{_apocope_uno(_tres_digitos_a_letras(millones))} MILLONES")
    miles, resto = divmod(resto, 1000)
    if miles:
        if miles == 1:
            partes.append("MIL")
        else:
            partes.append(f"{_apocope_uno(_tres_digitos_a_letras(miles))} MIL")
    if resto:
        partes.append(_tres_digitos_a_letras(resto))
    return " ".join(partes)


def amount_to_words_pen(amount):
    """Monto en soles a letras, formato peruano estándar de comprobantes:
    'SON: NOVENTA Y CINCO MIL QUINIENTOS OCHENTA Y 00/100 SOLES'."""
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        amount = 0.0
    entero = int(amount)
    centavos = round((amount - entero) * 100)
    if centavos == 100:
        entero += 1
        centavos = 0
    palabras = number_to_words_es(entero)
    return f"{palabras} Y {centavos:02d}/100 SOLES"


def company_info_for_issuer(issuer, cfg):
    """Datos de la empresa emisora según sea Harraso o BRMS. Mismo criterio
    ya usado en Cotizaciones (`quotations.issuer`, `_parse_issuer()` en
    cotizaciones.py/viajes.py): un documento guarda su empresa emisora al
    crearse y no se recalcula después. Se usa aquí para armar el
    comprobante que se manda a tefacturo.pe en Facturación y Guías — cada
    empresa tiene su propio RUC y, por lo tanto, necesita su propia cuenta
    (usuario/clave) ante tefacturo.pe (ver HARRASO_TEFACTURO_EMAIL/PASSWORD
    y BRMS_TEFACTURO_EMAIL/PASSWORD en config.py).

    `commercial_name`/`legal_name`/`email`/`mtc_registration` se agregaron
    el 7 sep (segunda ronda) para armar los campos `emisor`/`transportista`
    que exige el formato real de tefacturo.pe — antes este helper solo
    tenía ruc/name/address (suficiente para el intento anterior, basado en
    un manual incompleto). `warehouse_code` se agregó el 8 sep, tras el
    primer envío real de una guía: `datosDocumento.codigoAlmacen` es
    obligatorio y no existía ningún dato parecido en el sistema.
    `bank_nacion_detraction_account` se agregó el 9 sep, para el cálculo de
    detracción (ver `compute_detraction()` más abajo) — es la cuenta del
    Banco de la Nación de cada empresa, distinta de la cuenta bancaria
    "normal" que ya se usa en Cotizaciones (`COMPANY_BANK_NACION_ACCOUNT`/
    `BRMS_BANK_ACCOUNT`): para Harraso resulta ser la MISMA cuenta
    (confirmado contra una factura real ya emitida), pero BRMS necesita la
    suya propia (SUNAT asigna una cuenta de detracciones por RUC)."""
    if issuer == "BRMS":
        legal_suffix = ""  # razón social legal completa de BRMS aún sin confirmar (ver Cotizaciones)
        return {
            "ruc": cfg.get("BRMS_RUC", ""),
            "name": "BRMS",
            "address": cfg.get("BRMS_ADDRESS", ""),
            "email": cfg.get("BRMS_EMAIL", ""),  # 20 sep: BRMS tiene su propio correo (antes compartía el de Harraso)
            "commercial_name": "BRMS",
            "legal_name": f"BRMS {legal_suffix}".strip(),
            "mtc_registration": cfg.get("BRMS_MTC_REGISTRATION", ""),
            "warehouse_code": cfg.get("BRMS_WAREHOUSE_CODE", ""),
            "bank_nacion_detraction_account": cfg.get("BRMS_BANK_NACION_DETRACTION_ACCOUNT", ""),
        }
    return {
        "ruc": cfg.get("COMPANY_RUC", ""),
        "name": cfg.get("COMPANY_NAME", ""),
        "address": cfg.get("COMPANY_ADDRESS", ""),
        "email": cfg.get("COMPANY_EMAIL", ""),
        "commercial_name": cfg.get("COMPANY_NAME", ""),
        "legal_name": f"{cfg.get('COMPANY_NAME', '')} S.A.C.".strip(),
        "mtc_registration": cfg.get("HARRASO_MTC_REGISTRATION", ""),
        "warehouse_code": cfg.get("HARRASO_WAREHOUSE_CODE", ""),
        "bank_nacion_detraction_account": cfg.get("COMPANY_BANK_NACION_ACCOUNT", ""),
    }


# Detracción (SPOT) — 9 sep, Braulio compartió una factura real ya emitida
# (fuera de este ERP) que incluye el bloque "Concepto de Detracción": el
# servicio de transporte de bienes por vía terrestre está sujeto al 4% de
# detracción cuando el importe de la operación supera S/ 400 — código de
# bien "027" del catálogo SUNAT. Confirmado en DOS fuentes independientes:
# (1) la propia factura real que compartió Braulio (S/708.00 * 4% = S/28.32,
# coincide exacto con el monto mostrado), y (2) la orientación oficial de
# SUNAT
# (orientacion.sunat.gob.pe/detracciones-en-el-transporte-de-bienes-por-via-terrestre):
# "el monto del depósito resulta de aplicar el porcentaje de cuatro por
# ciento (4%) ... siempre que el importe de la operación ... sea mayor a
# S/.400.00".
#
# 22 sep, pedido de Braulio ("recuerda que solo Harraso emite con
# detraccion, BRMS no"): a diferencia de lo que se asumía antes (que
# Harraso y BRMS prestaban el mismo servicio y por tanto ambas caían bajo
# el mismo "027"), BRMS nunca aplica detracción — ni el cálculo automático
# de acá abajo ni la confirmación manual de facturación.py/detail.html se
# ofrecen para una factura de BRMS, sin importar el monto. Ver el filtro
# por `issuer` en app/routes/facturacion.py (new()) y el `{% if
# invoice.issuer == 'HARRASO' %}` en facturacion/detail.html.
DETRACTION_CODE = "027"
DETRACTION_PERCENTAGE = 4.0
DETRACTION_THRESHOLD = 400.0

# 22 sep, pedido de Braulio: catálogo de bienes/servicios para elegir por
# NOMBRE al confirmar la detracción de una factura con ítems manuales (no
# 100% viajes, que sigue calculando "027" solo) — el formulario ya no pide
# escribir el código a mano, se elige de esta lista y el código/porcentaje
# se completan solos (ver facturacion/form.html y facturacion/detail.html).
#
# 22 sep, 2da ronda, pedido de Braulio ("en catalogos hay que incluir
# conceptos de detraccion y en este se puedan agregar o modificar los
# conceptos o porcentajes"): esta lista dejó de ser la fuente de verdad —
# ahora vive en la tabla `detraction_concepts`, editable desde Catálogos →
# Conceptos de detracción (ver app/routes/catalogos.py). Esta constante
# `_DETRACTION_GOODS_SEED` se queda solo como el contenido con el que se
# siembra esa tabla la primera vez (ver _seed_detraction_concepts_sqlite/
# _postgres en app/db.py) — una vez sembrada, editar acá NO cambia nada;
# hay que editarlo desde Catálogos.
#
# Lista tal como la dio Braulio (los primeros códigos "principales" del
# Anexo de bienes de SUNAT). OJO -- al verificarla contra fuentes públicas
# actuales (docs.factpro.la/catalogos-sunat, estudiobonilla.pe) aparecieron
# varias diferencias que NO se corrigieron acá a propósito (se prefirió
# respetar la lista que dio Braulio antes que asumir cuál fuente tiene
# razón): "003 Alcohol etílico", "008 Madera", "014 Carnes y despojos
# comestibles" y "017 Harina/pellets de pescado" aparecen en esas fuentes
# como 4% (acá quedaron con el % que dio Braulio); "006 Algodón", "013
# Animales vivos" y "015 Abonos/cueros/pieles" aparecen en al menos una
# fuente como códigos YA DEROGADOS (sin vigencia desde ~2014). Confirma con
# tu contador antes de usar cualquiera de estos cuatro códigos o alguno de
# esos tres en una factura real -- un depósito de detracción con el
# porcentaje o código equivocado no se puede corregir después con SUNAT
# (ahora que es editable desde Catálogos, ya se puede corregir ahí mismo
# apenas tu contador confirme el valor correcto).
_DETRACTION_GOODS_SEED = [
    ("001", "Azúcar y melaza de caña", 10.0),
    ("003", "Alcohol etílico", 10.0),
    ("004", "Recursos hidrobiológicos", 4.0),
    ("005", "Maíz amarillo duro", 4.0),
    ("006", "Algodón", 10.0),
    ("007", "Caña de azúcar", 10.0),
    ("008", "Madera", 12.0),
    ("009", "Arena y piedra", 10.0),
    ("010", "Residuos, subproductos, desechos, recortes y desperdicios", 15.0),
    ("013", "Animales vivos", 10.0),
    ("014", "Carnes y despojos comestibles", 10.0),
    ("015", "Abonos, cueros y pieles de origen animal", 10.0),
    ("016", "Aceite de pescado", 10.0),
    ("017", "Harina, polvo y \"pellets\" de pescado", 10.0),
    (DETRACTION_CODE, "Transporte y/o traslado de bienes", DETRACTION_PERCENTAGE),
]


def get_detraction_concepts(only_active=True):
    """Filas de `detraction_concepts` (código, nombre, porcentaje editables
    desde Catálogos), ordenadas por sort_order/código."""
    sql = "SELECT * FROM detraction_concepts"
    if only_active:
        sql += " WHERE active = 1"
    sql += " ORDER BY sort_order, code"
    return query_all(sql)


def get_detraction_goods_catalog(only_active=True):
    """Mismo contenido que `get_detraction_concepts()`, como lista de
    tuplas (código, nombre, porcentaje) -- formato que ya esperan
    facturacion/form.html y facturacion/detail.html para el selector de
    bienes (`{% for code, label, pct in detraction_goods_catalog %}`), sin
    tener que tocar esos templates al pasar de la constante fija a la
    tabla editable."""
    return [(c["code"], c["name"], c["percentage"]) for c in get_detraction_concepts(only_active)]


def get_detraction_goods_codes(only_active=True):
    return {c["code"] for c in get_detraction_concepts(only_active)}


def get_detraction_percentage(code, default=None):
    """Porcentaje vigente de un código de detracción (activo) según
    Catálogos → Conceptos de detracción. Si el código no existe o fue
    desactivado, cae al `default` que le pases -- así, si alguien
    desactiva por error el código 027 (transporte de carga), el cálculo
    automático de facturas 100% viajes no se rompe en silencio, solo deja
    de reflejar un cambio de porcentaje que nunca llegó a guardarse."""
    row = query_one(
        "SELECT percentage FROM detraction_concepts WHERE code = ? AND active = 1", (code,)
    )
    return row["percentage"] if row else default


def get_detraction_tefacturo_code(code):
    """23 sep: el valor de `codigoBienServicio` que espera tefacturo.pe para
    este código nuestro de detracción (ver el comentario largo en
    schema.sql, junto a la columna `tefacturo_codigo_bien_servicio`). Puede
    no estar configurado todavía (columna en blanco) -- en ese caso
    devuelve None, y build_invoice_payload() (app/integrations/sunat_ose.py)
    NO manda el bloque "detraccion" para esa factura (mismo criterio
    conservador de siempre: mejor no enviarlo que adivinar mal un campo de
    SUNAT)."""
    row = query_one(
        "SELECT tefacturo_codigo_bien_servicio FROM detraction_concepts WHERE code = ?", (code,)
    )
    value = row["tefacturo_codigo_bien_servicio"] if row else None
    return value.strip() if value and value.strip() else None


def compute_detraction(amount, company):
    """Calcula si una factura está sujeta a detracción y, si aplica, su
    monto — ver el comentario de las constantes DETRACTION_* arriba.
    `amount` es el importe TOTAL de la operación (con IGV incluido, igual
    que `invoices.amount`) — la detracción se calcula sobre ese total, no
    sobre el valor de venta sin IGV (confirmado contra la factura real:
    S/708 con IGV, no S/600 sin IGV, es la base del 4%). `company` es el
    dict de `company_info_for_issuer()`, para tomar la cuenta del Banco de
    la Nación correspondiente.

    Devuelve un dict con `applies`, `code`, `percentage`, `amount` (monto
    detraído) y `bank_account` — pensado para pasarse directo a la
    inserción de la factura (ver app/routes/facturacion.py)."""
    amount = float(amount or 0)
    applies = amount > DETRACTION_THRESHOLD
    if not applies:
        return {
            "applies": False,
            "code": None,
            "percentage": None,
            "amount": None,
            "bank_account": None,
        }
    # 22 sep, 2da ronda: el porcentaje del código 027 ahora puede editarse
    # desde Catálogos → Conceptos de detracción -- se toma de ahí, cayendo
    # a la constante DETRACTION_PERCENTAGE de arriba solo si ese código no
    # existe o está desactivado en la tabla (ver get_detraction_percentage).
    percentage = get_detraction_percentage(DETRACTION_CODE, default=DETRACTION_PERCENTAGE)
    detraction_amount = round(amount * percentage / 100, 2)
    return {
        "applies": True,
        "code": DETRACTION_CODE,
        "percentage": percentage,
        "amount": detraction_amount,
        "bank_account": company.get("bank_nacion_detraction_account", ""),
    }


def pretty_label(value):
    """Convierte códigos tipo 'COMBUSTIBLE' o 'en_curso' en texto legible
    ('Combustible', 'En Curso'). Si el valor ya viene con formato humano
    (por ejemplo un concepto agregado desde Catálogos), lo deja tal cual."""
    if not value:
        return ""
    return value.replace("_", " ").title()


# Tamaño y calidad para fotos de conductores (1 sep) — mismo criterio que
# los comprobantes de gastos (ver RECEIPT_MAX_DIMENSION en
# app/routes/liquidaciones.py), pero más chico: es una foto tipo carné, no
# un documento que haya que leer con detalle.
PHOTO_MAX_DIMENSION = 800
PHOTO_JPEG_QUALITY = 78


def compress_photo(raw_bytes):
    """Redimensiona y recomprime una foto como JPEG (ver
    _compress_receipt_image en liquidaciones.py, mismo criterio). Devuelve
    los bytes JPEG ya comprimidos, o None si el archivo no se pudo abrir
    como imagen, en cuyo caso el llamador debe decidir qué hacer (guardar
    el original, o rechazarlo)."""
    try:
        with Image.open(io.BytesIO(raw_bytes)) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.thumbnail((PHOTO_MAX_DIMENSION, PHOTO_MAX_DIMENSION), Image.LANCZOS)
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=PHOTO_JPEG_QUALITY, optimize=True)
            return buffer.getvalue()
    except Exception:
        return None
