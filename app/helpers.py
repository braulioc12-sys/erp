"""Funciones auxiliares compartidas por las rutas."""
from datetime import datetime

from app.db import query_one


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


def pretty_label(value):
    """Convierte códigos tipo 'COMBUSTIBLE' o 'en_curso' en texto legible
    ('Combustible', 'En Curso'). Si el valor ya viene con formato humano
    (por ejemplo un concepto agregado desde Catálogos), lo deja tal cual."""
    if not value:
        return ""
    return value.replace("_", " ").title()
