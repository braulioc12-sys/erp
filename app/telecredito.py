"""Generador del archivo de texto de ancho fijo para Telecrédito Web (BCP)
— Planilla de Haberes, usada tanto para Planilla como para Recibo por
honorarios.

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
valor que trae el archivo real (1705882738935).

18 sep, 3ra ronda (corrección de Braulio, patch 0054): al principio se
había armado también un formato "Proveedores" distinto (196 caracteres de
fila, con "Modalidad de pago" y flag de exoneración ITF) para los pagos de
RECIBO_HONORARIOS, leyendo la ficha "Estructura planilla Proveedores NTLC
2.pdf" — pero al subirlo a Telecrédito real dio error. Braulio aclaró:

    "A la hora de generar el pago de honorarios lo estas usandi como si
    usara el formato de proveedores, pero es igual el mismo formato que
    haberes solo cambia el campo 4 tipo de subtipo."

Es decir: BCP factura los honorarios como una PLANILLA DE HABERES más,
solo que con el campo 4 de la cabecera ("Subtipo de planilla de haberes")
en "4" (CUARTA CATEGORÍA) en vez de "X" (QUINTA CATEGORÍA, el sueldo
regular). Por eso ya no existe una función aparte para Proveedores: tanto
Planilla como Recibo por honorarios arman el archivo con
`build_haberes_txt()`, y lo único que cambia entre los dos es el valor del
subtipo (ver DEFAULT_SUBTIPO_PLANILLA vs. DEFAULT_SUBTIPO_HONORARIOS más
abajo) y el texto por defecto del concepto/referencia. Como consecuencia,
el documento del beneficiario también queda limitado a DNI/CE igual que
Planilla (la ficha de Haberes no tiene código de RUC) — si algún
proveedor de honorarios tiene RUC en vez de DNI/CE, ese pago se excluye
del archivo con aviso (mismo criterio que ya existía para moneda
distinta), igual que antes.

Único formato (ficha oficial BCP, julio 2024): 113 caracteres de cabecera
+ filas de pago de 195 caracteres. Lleva "Subtipo de planilla de haberes"
en la cabecera (ej. "X" = Quinta categoría para un sueldo mensual normal —
es el que usa Braulio en su archivo real de Planilla — o "4" = Cuarta
categoría para honorarios). Tipo de documento del beneficiario: solo
DNI/CE (no admite RUC).

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

# G/V/M/P/T/4/O/X/Z tal cual la ficha "Estructura de la Planilla de
# Haberes" de BCP. "X" es el default (Quinta categoría) porque es el que
# usa Braulio en su archivo real para el sueldo mensual normal de
# Planilla; "4" (Cuarta categoría) es el que corresponde a Recibo por
# honorarios (ver DEFAULT_SUBTIPO_HONORARIOS) — mismo listado, se ofrece
# completo por si algún día se necesita otro subtipo (ej. Gratificación).
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
DEFAULT_SUBTIPO_HONORARIOS = "4"

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


def build_haberes_txt(payments, *, cuenta_cargo, fecha_proceso, subtipo_planilla, referencia_planilla, default_company_name, default_concept="PAGO DE HABERES"):
    """payments: filas ya resueltas con bank_type/bank_value/bank_is_interbank
    (ver _prepare_export_rows en app/routes/pagos_personal.py) además de
    document_type, document_number, staff_name, staff_company,
    staff_currency, amount, concept. cuenta_cargo: fila de
    company_bank_accounts (account_number, account_type, currency).
    Usada tanto para Planilla (subtipo_planilla="X" por defecto) como para
    Recibo por honorarios (subtipo_planilla="4") — es el mismo formato de
    archivo, lo único que cambia es ese campo de la cabecera y el texto
    por defecto del concepto (ver DEFAULT_SUBTIPO_PLANILLA /
    DEFAULT_SUBTIPO_HONORARIOS y el docstring del módulo). Devuelve bytes
    listos para descargar como .txt (latin-1, CRLF, igual que el archivo
    real de Braulio)."""
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
            + _text_field(p["concept"] or default_concept, 20)
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
