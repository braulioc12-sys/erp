"""Generador del archivo de texto de ancho fijo para Telecrédito Web (BCP)
— Planilla de Haberes y Planilla de Proveedores.

18 sep (pedido de Braulio, en dos partes):

    1) "creemos un modulo mas que se llame Pagos personal ... Quiero que
       haya la opcion de que cree un excel con los formatos que usa la
       plataforma Telecredito de BCP" (patch 0051 — se entregó un Excel
       "borrador" porque todavía no se tenía la plantilla real).
    2) "te paso un archivo masivo de haberes para que tengas el ejemplo" +
       las 2 fichas oficiales de BCP ("Estructura de la Planilla de
       Haberes" / "...de Proveedores").

Este módulo reemplaza aquel Excel "borrador" por el archivo de texto
EXACTO que pide el banco (cabecera + filas de pago, con el checksum
incluido) — verificado byte a byte contra el archivo real que mandó
Braulio (CUMBAZAGOSTO26.TXT, 23 abonos): se decodificaron sus posiciones a
mano, se confirmaron contra la ficha oficial de BCP, y se recalculó el
checksum de sus 23 cuentas + la cuenta de cargo dando EXACTO el mismo
valor que trae el archivo real (1705882738935). Con esa verificación, el
archivo que genera este módulo ya no lleva ninguna advertencia de
"borrador".

Dos formatos (fichas oficiales BCP, ambas de julio 2024), los dos con 113
caracteres de cabecera pero con las filas de pago y algunos campos de la
cabecera en posiciones distintas:

- HABERES (planilla de sueldos, PLANILLA en el sistema): fila de pago de
  195 caracteres. Lleva "Subtipo de planilla" en la cabecera (ej. "X" =
  Quinta categoría, el caso normal de un sueldo mensual — es el que usa
  Braulio en su archivo real). Tipo de documento del trabajador: solo
  DNI/CE (no admite RUC).
- PROVEEDORES (honorarios/terceros, RECIBO_HONORARIOS en el sistema): fila
  de pago de 196 caracteres (un campo más que Haberes: "Modalidad de
  pago", siempre "1" = Efectivo, la única implementada). Lleva "Flag de
  exoneración ITF" en la cabecera en vez del subtipo. Tipo de documento
  del proveedor: DNI/CE/RUC.

Todos los campos de texto libre (nombre, referencias) se sanitizan
--mayúsculas, sin tildes-- porque las recomendaciones generales de BCP
piden evitar acentos y caracteres especiales; la Ñ se conserva porque el
propio archivo real de Braulio la usa tal cual (ej. "MIÑAN", "NIÑO") y el
patrón de caracteres permitidos de la ficha la incluye.
"""
import re

CURRENCY_CODES = {"S": "0001", "D": "1001"}
CURRENCY_LABELS_SHORT = {"S": "Soles", "D": "Dólares"}

# Tipo de cuenta de ABONO (la de cada persona que recibe el pago).
ACCOUNT_TYPE_CODES_ABONO = {"AHORROS": "A", "CORRIENTE": "C", "MAESTRA": "M"}
# Tipo de cuenta de CARGO (la de la empresa que paga) — BCP solo admite
# Corriente o Maestra para la cuenta de cargo, nunca Ahorros, por eso
# company_bank_accounts.account_type no ofrece "AHORROS" como opción (ver
# app/routes/catalogos.py, bancos_add()).
ACCOUNT_TYPE_CODES_CARGO = {"CORRIENTE": "C", "MAESTRA": "M"}

DOCUMENT_TYPE_CODES_HABERES = {"DNI": "1", "CE": "3"}
DOCUMENT_TYPE_CODES_PROVEEDORES = {"DNI": "1", "CE": "3", "RUC": "6"}

# G/V/M/P/T/4/O/X/Z tal cual la ficha "Estructura de la Planilla de
# Haberes" de BCP. "X" es el default (Quinta categoría) porque es el que
# usa Braulio en su archivo real para el sueldo mensual normal.
SUBTIPO_PLANILLA_CHOICES = [
    ("X", "Quinta categoría (sueldo regular)"),
    ("G", "Gratificación"),
    ("V", "Vacaciones"),
    ("M", "Movilidad"),
    ("P", "Pensionista"),
    ("T", "Préstamos"),
    ("4", "Cuarta categoría"),
    ("O", "Otros afectos"),
    ("Z", "Otros inafectos"),
]
DEFAULT_SUBTIPO_PLANILLA = "X"

_ACCENTS = str.maketrans("áéíóúÁÉÍÓÚ", "aeiouAEIOU")
# Caracteres permitidos por BCP en nombre/referencia (ficha oficial):
# ^[a-zA-ZáéíóúÁÉÍÓÚñÑýÝ\-.()#/@&]*$ — pero las "Recomendaciones generales"
# de la misma ficha piden evitar acentos y usar mayúsculas, así que acá se
# transliteran las vocales acentuadas y se deja todo en mayúsculas; la Ñ
# SÍ se conserva (permitida, y aparece tal cual en el archivo real).
_DISALLOWED_CHARS = re.compile(r"[^A-Z0-9ÑÝ\-.()#/@& ]")


def _sanitize_text(value):
    value = (value or "").translate(_ACCENTS).upper()
    return _DISALLOWED_CHARS.sub("", value)


def _digits_only(value):
    return re.sub(r"\D", "", str(value or ""))


def _alnum_upper(value):
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def _num_field(value, length):
    """Campo numérico: alineado a la derecha, cero a la izquierda."""
    digits = _digits_only(value) or "0"
    return digits[-length:].rjust(length, "0")


def _account_field(value, length=20):
    """Cuenta/CCI: solo dígitos, alineado a la IZQUIERDA, espacios a la
    derecha (así lo pide la ficha para cuenta de cargo y cuenta de
    abono)."""
    digits = _digits_only(value)
    return digits[:length].ljust(length)


def _alnum_field(value, length):
    """Documento (puede tener letras, ej. Carné de Extranjería): alineado
    a la izquierda, espacios a la derecha."""
    return _alnum_upper(value)[:length].ljust(length)


def _text_field(value, length):
    """Texto libre (nombre, referencias): sanitizado, alineado a la
    izquierda, espacios a la derecha."""
    return _sanitize_text(value)[:length].ljust(length)


def _amount_field(value, length=17):
    """Monto formato XXXXXXXXXXXXXXX.YY: alineado a la derecha, cero a la
    izquierda. length incluye el punto y los 2 decimales."""
    whole, _, dec = f"{float(value or 0):.2f}".partition(".")
    whole = whole.lstrip("-")
    return whole.rjust(length - 3, "0") + "." + dec


def _date_field(date_str):
    """'YYYY-MM-DD' (como lo da un <input type=date>) -> 'YYYYMMDD'."""
    digits = _digits_only(date_str)
    return digits[:8].ljust(8, "0")


def _checksum_component(value, is_interbank):
    """Quita los primeros 3 dígitos (sucursal) de una cuenta BCP propia, o
    los primeros 10 de una cuenta interbancaria (CCI) — tal cual el
    algoritmo de la ficha oficial, verificado contra el archivo real de
    Braulio (ver el docstring del módulo)."""
    digits = _digits_only(value)
    stripped = digits[10:] if is_interbank else digits[3:]
    return int(stripped) if stripped else 0


def compute_checksum(cuenta_cargo, abonos):
    """abonos: lista de (cuenta_o_cci, is_interbank). Devuelve el entero
    checksum (sin el zero-padding final, eso lo hace _num_field)."""
    total = _checksum_component(cuenta_cargo, is_interbank=False)
    for value, is_interbank in abonos:
        total += _checksum_component(value, is_interbank)
    return total


def abono_bank_fields(cci, account_number, account_type):
    """A partir de los datos bancarios de una persona, decide qué va en
    "tipo de cuenta de abono" y en el campo de cuenta: si tiene CCI
    registrado se usa esa (interbancaria, tipo "B"); si no, su cuenta
    propia con el tipo que tenga guardado (Ahorros por defecto — es lo que
    usa Braulio para todo el personal en su archivo real). Devuelve
    (tipo_cuenta_code, valor_cuenta, es_interbancaria) o (None, None,
    None) si no hay ningún dato bancario usable."""
    cci_digits = _digits_only(cci)
    if cci_digits:
        return "B", cci_digits, True
    account_digits = _digits_only(account_number)
    if account_digits:
        tipo = ACCOUNT_TYPE_CODES_ABONO.get((account_type or "AHORROS").upper(), "A")
        return tipo, account_digits, False
    return None, None, None


def _crlf_bytes(lines):
    return ("\r\n".join(lines) + "\r\n").encode("latin-1", errors="replace")


def build_haberes_txt(payments, *, cuenta_cargo, fecha_proceso, subtipo_planilla, referencia_planilla, default_company_name):
    """payments: filas ya resueltas con bank_type/bank_value/bank_is_interbank
    (ver _prepare_export_rows en app/routes/pagos_personal.py) además de
    document_type, document_number, staff_name, staff_company,
    staff_currency, amount, concept. cuenta_cargo: fila de
    company_bank_accounts (account_number, account_type, currency).
    Devuelve bytes listos para descargar como .txt (latin-1, CRLF, igual
    que el archivo real de Braulio)."""
    total_amount = sum(p["amount"] or 0 for p in payments)
    abonos_checksum = [(p["bank_value"], p["bank_is_interbank"]) for p in payments]

    rows = []
    for p in payments:
        doc_code = DOCUMENT_TYPE_CODES_HABERES.get(p["document_type"], "1")
        company_name = p["staff_company"] or default_company_name
        row = (
            "2"
            + p["bank_type"]
            + _account_field(p["bank_value"])
            + doc_code
            + _alnum_field(p["document_number"], 12)
            + " " * 3
            + _text_field(p["staff_name"], 75)
            + _text_field(company_name, 40)
            + _text_field(p["concept"] or "PAGO DE HABERES", 20)
            + CURRENCY_CODES.get(p["staff_currency"], "0001")
            + _amount_field(p["amount"])
            + "S"
        )
        rows.append(row)

    header = (
        "1"
        + _num_field(len(payments), 6)
        + _date_field(fecha_proceso)
        + subtipo_planilla
        + ACCOUNT_TYPE_CODES_CARGO.get(cuenta_cargo["account_type"], "C")
        + CURRENCY_CODES.get(cuenta_cargo["currency"], "0001")
        + _account_field(cuenta_cargo["account_number"])
        + _amount_field(total_amount)
        + _text_field(referencia_planilla, 40)
        + _num_field(compute_checksum(cuenta_cargo["account_number"], abonos_checksum), 15)
    )

    return _crlf_bytes([header] + rows)


def build_proveedores_txt(payments, *, cuenta_cargo, fecha_proceso, referencia_planilla, exonerar_itf, default_company_name):
    """Igual que build_haberes_txt pero con la estructura de la Planilla
    de Proveedores: sin subtipo de planilla, con flag de exoneración ITF
    en la cabecera, y "modalidad de pago" (siempre Efectivo) + documento
    RUC habilitado en cada fila de pago."""
    total_amount = sum(p["amount"] or 0 for p in payments)
    abonos_checksum = [(p["bank_value"], p["bank_is_interbank"]) for p in payments]

    rows = []
    for p in payments:
        doc_code = DOCUMENT_TYPE_CODES_PROVEEDORES.get(p["document_type"], "1")
        company_name = p["staff_company"] or default_company_name
        row = (
            "2"
            + p["bank_type"]
            + _account_field(p["bank_value"])
            + "1"  # Modalidad de pago: 1 = Efectivo (única implementada por BCP)
            + doc_code
            + _alnum_field(p["document_number"], 12)
            + " " * 3
            + _text_field(p["staff_name"], 75)
            + _text_field(company_name, 40)
            + _text_field(p["concept"] or "PAGO HONORARIOS", 20)
            + CURRENCY_CODES.get(p["staff_currency"], "0001")
            + _amount_field(p["amount"])
            + "S"
        )
        rows.append(row)

    header = (
        "1"
        + _num_field(len(payments), 6)
        + _date_field(fecha_proceso)
        + ACCOUNT_TYPE_CODES_CARGO.get(cuenta_cargo["account_type"], "C")
        + CURRENCY_CODES.get(cuenta_cargo["currency"], "0001")
        + _account_field(cuenta_cargo["account_number"])
        + _amount_field(total_amount)
        + _text_field(referencia_planilla, 40)
        + ("S" if exonerar_itf else "N")
        + _num_field(compute_checksum(cuenta_cargo["account_number"], abonos_checksum), 15)
    )

    return _crlf_bytes([header] + rows)
