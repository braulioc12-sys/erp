"""Lee archivos .txt de Telecrédito Web (BCP) YA GENERADOS (por el banco o
por este mismo sistema en un mes anterior) y extrae los datos bancarios de
cada persona que aparece en las filas de pago — para completar el
Catálogo de Personal en bloque sin tener que tipear cuenta por cuenta (18
sep, 6ta ronda, pedido de Braulio: "Si te adjunto varios .txt que usa
telecredito y ya sabes como usa su estructura, puedes agregar los datos
del personal?").

Es el reverso exacto de build_haberes_txt() en app/telecredito.py — mismas
posiciones de columna, ya verificadas byte a byte contra un archivo real
(ver el docstring de ese módulo). Como Planilla y Recibo por honorarios
comparten el mismo formato de fila desde el patch 0054, este lector sirve
para cualquiera de los dos tipos de archivo sin distinción.

Deliberadamente NO se intenta extraer "empresa" (posición 113-153 de la
fila): ese campo puede traer el nombre de la empresa que figura como
empleadora de la persona, pero también puede traer simplemente el nombre
de la cuenta de cargo (cuando la persona no tenía "empresa" puesta) — no
hay forma de distinguir un caso del otro solo mirando el archivo, así que
se deja ese dato para que Braulio lo revise a mano si hace falta, en vez
de arriesgarse a guardar algo incorrecto."""
import re

DOCUMENT_TYPE_FROM_CODE = {"1": "DNI", "3": "CE"}
CURRENCY_FROM_CODE = {"0001": "S", "1001": "D"}
ACCOUNT_TYPE_FROM_ABONO_CODE = {"A": "AHORROS", "C": "CORRIENTE", "M": "MAESTRA"}

_ROW_LENGTH = 195
_HEADER_LENGTH = 113


def _decode_text(raw_bytes):
    """Mismo criterio que _crlf_bytes() en app/telecredito.py, al revés:
    el archivo se escribió en latin-1."""
    return raw_bytes.decode("latin-1", errors="replace")


def parse_txt_file(raw_bytes):
    """Devuelve (header_info, rows, warnings). header_info es un dict con
    'subtipo' (para saber si el archivo es de Planilla o de Honorarios) o
    None si no se pudo leer la cabecera. rows es una lista de dicts, uno
    por cada fila de pago reconocida: name, document_type, document_number,
    bank_value (cuenta o CCI), is_cci, account_type, currency. warnings es
    una lista de strings (líneas que no se pudieron leer)."""
    text = _decode_text(raw_bytes)
    lines = [ln for ln in re.split(r"\r\n|\r|\n", text) if ln.strip()]

    header_info = None
    rows = []
    warnings = []

    for i, line in enumerate(lines, start=1):
        if not line:
            continue
        marker = line[0]
        if marker == "1":
            if len(line) < _HEADER_LENGTH:
                warnings.append(f"Línea {i}: cabecera más corta de lo esperado, se ignoró.")
                continue
            header_info = {"subtipo": line[15]}
            continue
        if marker != "2":
            continue  # línea vacía o de otro tipo, se ignora sin avisar
        if len(line) < _ROW_LENGTH:
            warnings.append(f"Línea {i}: fila de pago más corta de lo esperado ({len(line)} caracteres), se ignoró.")
            continue

        bank_type = line[1]
        bank_value_raw = line[2:22].strip()
        doc_code = line[22]
        document_number = line[23:35].strip()
        name = line[38:113].strip()
        currency_code = line[173:177]

        if not name:
            warnings.append(f"Línea {i}: sin nombre, se ignoró.")
            continue
        if not bank_value_raw:
            warnings.append(f"Línea {i} ({name}): sin cuenta ni CCI, se ignoró.")
            continue

        is_cci = bank_type == "B"
        account_type = None if is_cci else ACCOUNT_TYPE_FROM_ABONO_CODE.get(bank_type, "AHORROS")

        rows.append({
            "line": i,
            "name": name,
            "document_type": DOCUMENT_TYPE_FROM_CODE.get(doc_code, "DNI"),
            "document_number": document_number or None,
            "bank_value": bank_value_raw,
            "is_cci": is_cci,
            "account_type": account_type,
            "currency": CURRENCY_FROM_CODE.get(currency_code, "S"),
        })

    if not rows and header_info is None:
        warnings.append("No se reconoció ninguna cabecera ni fila de pago en este archivo — ¿es un .txt real de Telecrédito?")

    return header_info, rows, warnings
