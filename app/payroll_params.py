"""Parámetros legales de la planilla (6 oct, pedido de Braulio: AFP, ONP,
5ta categoría, CTS, gratificación, EsSalud...). Estos valores cambian (UIT y
RMV cada año; la remuneración máxima asegurable de la AFP y las comisiones
cada trimestre/mes), así que NO van fijos en el código: se siembran en la
tabla `payroll_params` con los vigentes de 2026 y se corrigen desde la
pantalla "Parámetros de planilla".

Fuentes consultadas el 6 oct 2026 (confirmar siempre con el contador / la SBS
antes de pagar): UIT 2026 S/ 5,500 (D.S. 301-2025-EF); RMV S/ 1,130
(D.S. 006-2024-TR); AFP: aporte obligatorio 10% y prima de seguro 1.37% igual
para las 4 AFP, comisión por flujo Habitat 1.47%, Integra 1.55%, Prima 1.60%,
Profuturo 1.69% (SBS, devengue mayo 2026); remuneración máxima asegurable
S/ 12,672.65 para jul–sep 2026 (SBS, cambia cada trimestre); ONP 13%;
EsSalud 9% a cargo del empleador.
"""
from app.db import execute, query_all, query_one
from app.helpers import now_str

# (clave, etiqueta, valor sugerido, fuente / nota)
DEFAULT_PARAMS = [
    ("uit", "UIT (S/)", 5500.0, "D.S. 301-2025-EF (UIT 2026)"),
    ("rmv", "Remuneración mínima vital (S/)", 1130.0, "D.S. 006-2024-TR"),
    ("asig_fam_pct", "Asignación familiar (% de la RMV)", 10.0, "Ley 25129"),
    ("essalud_pct", "EsSalud a cargo del empleador (%)", 9.0, "Ley 26790"),
    ("sis_micro_monto", "Seguro de salud MYPE microempresa (S/ por mes, a cargo del empleador)", 15.0, "Ley MYPE; el empleador paga este monto fijo en lugar del 9%"),
    ("onp_pct", "ONP (%)", 13.0, "D.L. 19990"),
    ("afp_aporte_pct", "AFP – aporte obligatorio al fondo (%)", 10.0, "SBS"),
    ("afp_seguro_pct", "AFP – prima de seguro (%)", 1.37, "SBS (igual para las 4 AFP)"),
    ("afp_rma", "AFP – remuneración máxima asegurable (S/), tope de la prima de seguro", 12672.65, "SBS, vigente jul–sep 2026; cambia cada trimestre"),
    ("afp_flujo_HABITAT", "AFP Habitat – comisión sobre la remuneración (%)", 1.47, "SBS, mayo 2026"),
    ("afp_flujo_INTEGRA", "AFP Integra – comisión sobre la remuneración (%)", 1.55, "SBS, mayo 2026"),
    ("afp_flujo_PRIMA", "AFP Prima – comisión sobre la remuneración (%)", 1.60, "SBS, mayo 2026"),
    ("afp_flujo_PROFUTURO", "AFP Profuturo – comisión sobre la remuneración (%)", 1.69, "SBS, mayo 2026"),
    ("quinta_deduccion_uit", "Renta de 5ta categoría – deducción anual (en UIT)", 7.0, "Ley del Impuesto a la Renta, art. 46"),
    ("grati_bonif_pct", "Bonificación extraordinaria sobre la gratificación (%)", 9.0, "Ley 30334 (reemplaza el aporte a EsSalud en las gratificaciones)"),
    ("dias_mes", "Días del mes para calcular descuentos (base 30)", 30.0, "Se divide el sueldo entre 30 para el valor del día"),
    ("horas_dia", "Horas de la jornada diaria (para descontar tardanzas por hora)", 8.0, "Jornada máxima legal 8 horas"),
    ("incap_dias_empleador", "Días de descanso médico por año que paga el empleador", 20.0, "Del día 21 en adelante el subsidio lo paga EsSalud"),
    ("subsidio_descuenta", "¿Descontar en planilla los días que cubre el subsidio de EsSalud? (1 = sí, 0 = no)", 1.0, "1 si EsSalud le paga directo al trabajador; 0 si el empleador adelanta y luego recupera"),
]
LABELS = {k: (label, src) for k, label, _v, src in DEFAULT_PARAMS}
DEFAULTS = {k: v for k, _l, v, _s in DEFAULT_PARAMS}

AFP_NAMES = [("HABITAT", "AFP Habitat"), ("INTEGRA", "AFP Integra"), ("PRIMA", "AFP Prima"), ("PROFUTURO", "AFP Profuturo")]


def get_params():
    """Dict clave -> valor con lo guardado; lo que falte usa el valor sugerido."""
    params = dict(DEFAULTS)
    for r in query_all("SELECT key, value FROM payroll_params"):
        params[r["key"]] = r["value"]
    return params


def get_param_rows():
    saved = {r["key"]: r for r in query_all("SELECT key, value, updated_at FROM payroll_params")}
    rows = []
    for key, label, default, src in DEFAULT_PARAMS:
        r = saved.get(key)
        rows.append({
            "key": key, "label": label, "value": r["value"] if r else default, "default": default,
            "source": src, "updated_at": r["updated_at"] if r else None,
        })
    return rows


def set_param(key, value, user_id=None):
    if key not in DEFAULTS:
        raise KeyError(key)
    exists = query_one("SELECT key FROM payroll_params WHERE key = ?", (key,))
    if exists:
        execute("UPDATE payroll_params SET value = ?, updated_at = ?, updated_by = ? WHERE key = ?",
                (value, now_str(), user_id, key))
    else:
        execute("INSERT INTO payroll_params (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)",
                (key, value, now_str(), user_id))
