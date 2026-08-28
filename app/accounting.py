"""Definiciones centralizadas para la "liquidación contable" exportable
desde Gastos — el formato real que usa Harraso para pasarle sus gastos a
contabilidad (ver su plantilla "LIQ. BRMS" y su hoja "COLUMNAS
LIQUIDACION"). Igual que app/tire_positions.py o app/detailed_checklists.py,
todo lo que depende de convenciones propias de Harraso vive en un solo
archivo — si cambian una cuenta contable o agregan una oficina nueva, es
el único lugar que hay que tocar.

Cómo se arma cada liquidación exportada: cada anticipo de viáticos
liquidado (`expense_advances`, ver app/routes/liquidaciones.py) genera una fila
"Haber" (el vale entregado al conductor, contra la cuenta "por liquidar"
de su oficina) más una fila "Debe" por cada gasto documentado que se le
vinculó al liquidar (`expenses.expense_advance_id`). Los gastos que no
están ligados a ningún anticipo (por ejemplo gastos directos de una
unidad, sin viaje) no entran en este export — es específicamente el
formato de liquidación de viáticos, no un reporte general de gastos (para
eso está el reporte de Gastos por tipo/fecha que ya existía).
"""

# Oficina donde se liquida cada anticipo de viáticos. Cada una tiene su
# propio código de "Origen" (columna del export) y su propia cuenta
# contable de "por liquidar" (la que se usa en la fila Haber del vale).
# Origen y cuenta de Lima/Pucallpa confirmados por Braulio y por la hoja
# DATOS de su plantilla real. El código de Tarapoto (16) y su cuenta
# (14133) se tomaron solo de la hoja DATOS —siguiendo el mismo patrón de
# numeración que Lima/Pucallpa— pero Braulio no los confirmó
# explícitamente: avisarle si no son correctos.
OFFICES = {
    "LIMA": {"label": "Lima", "origen_code": "14", "cuenta_vale": "14131"},
    "PUCALLPA": {"label": "Pucallpa", "origen_code": "15", "cuenta_vale": "14132"},
    "TARAPOTO": {"label": "Tarapoto", "origen_code": "16", "cuenta_vale": "14133"},  # AJUSTAR: confirmar con Braulio
}

# Tipos de documento SUNAT usados por Harraso (hoja DATOS de su plantilla).
# "PL" (Por Liquidar) no es un documento fiscal — es el código interno que
# usan para el vale/anticipo entregado al conductor.
DOCUMENT_TYPES = [
    ("01", "Factura"),
    ("02", "Recibo por Honorario"),
    ("03", "Boleta de Venta"),
    ("RI", "Recibo de Ingreso"),
    ("PL", "Por Liquidar (vale)"),
]

VALE_DOCUMENT_TYPE = "PL"

# Encabezados EXACTOS de la "hoja resumen" de la plantilla real de
# Harraso (incluye espacios finales tal como están en su archivo) — el
# export de liquidación debe generarlos en este mismo orden y con estos
# mismos nombres, para que se pueda pegar directo en su sistema contable.
RESUMEN_COLUMNS = [
    "Origen",
    "Num.Voucher",
    "Fecha de Liquidacion   ",
    "Cuenta   ",
    "Monto Debe",
    "Monto Haber",
    "Moneda S/D ",
    "T.Cambio",
    "Doc",
    "Num.Doc     ",
    "Fec.Doc     ",
    "Fec.Ven    ",
    "RUC O DNI",
    "Glosa     ",
    "RUC O DNI        ",
    "R. Social",
]

DEFAULT_CURRENCY = "S"


def office_choices():
    """[(code, info), ...] en un orden estable para los <select>."""
    return [("LIMA", OFFICES["LIMA"]), ("PUCALLPA", OFFICES["PUCALLPA"]), ("TARAPOTO", OFFICES["TARAPOTO"])]


def office_info(office_code):
    return OFFICES.get(office_code)


def voucher_label(voucher_number):
    """01, 02, 03... — así aparece en la columna Num.Voucher del export."""
    try:
        return f"{int(voucher_number):02d}"
    except (TypeError, ValueError):
        return ""
