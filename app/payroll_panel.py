"""Panel del periodo de planilla (7 oct, idea tomada de Buk): totales, estado
de los archivos que se generan (boletas, Excel, resumen PLAME, resumen AFPnet,
transferencia bancaria / Telecrédito) y alertas (errores y advertencias) que
conviene revisar antes de cerrar.
"""
from flask import url_for

from app.db import execute, query_all, query_one
from app.helpers import now_str
from app.payroll_calc import item_afecto, r2, worked_days

# Avisos del cálculo (payroll_lines.warnings) que impiden pagar bien: se
# muestran como ERRORES; el resto de avisos son advertencias.
ERROR_PREFIXES = ("Sin sueldo básico", "Tiene AFP pero", "El neto sale negativo")

FILE_KINDS = [
    ("BOLETAS", "Boletas de pago (imprimir / PDF)"),
    ("EXCEL", "Planilla en Excel"),
    ("PLAME", "Resumen para PLAME"),
    ("AFPNET", "Resumen para AFPnet"),
]


def record_export(period, kind, user=None):
    execute(
        "INSERT INTO payroll_exports (period_id, kind, generated_at, generated_by) VALUES (?, ?, ?, ?)",
        (period["id"], kind, now_str(), user["id"] if user else None),
    )


def export_status(period, kind, has_lines):
    """('NONE'|'OK'|'STALE'|'WAIT', texto): estado del archivo `kind`."""
    if not has_lines:
        return "WAIT", "Calcula la planilla primero"
    row = query_one(
        "SELECT generated_at FROM payroll_exports WHERE period_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
        (period["id"], kind),
    )
    if row is None:
        return "NONE", "No generado"
    at = row["generated_at"]
    calc = period["calculated_at"]
    if calc and at < calc:
        return "STALE", f"Desactualizado (generado {at[:16]}, recalculado {calc[:16]})"
    return "OK", f"Actualizado ({at[:16]})"


def transfer_status(period, has_lines):
    if not has_lines:
        return "WAIT", "Calcula la planilla primero"
    if period["status"] == "ABIERTO":
        return "WAIT", "Los pagos se generan al cerrar la planilla"
    rows = query_all(
        """SELECT status, exported_at FROM staff_payments WHERE payment_type = 'PLANILLA' AND period = ?
           AND staff_id IN (SELECT staff_id FROM payroll_lines WHERE period_id = ?)""",
        (period["period"], period["id"]),
    )
    if not rows:
        return "NONE", "Sin pagos generados"
    paid = sum(1 for r in rows if r["status"] == "PAGADO")
    exported = sum(1 for r in rows if r["exported_at"])
    state = "OK" if paid == len(rows) else "NONE"
    return state, f"{paid} de {len(rows)} pagados · {exported} incluidos en un archivo de Telecrédito"


def period_alerts(p, lines):
    """Errores y advertencias del periodo: [{'text', 'url'}, ...] cada una."""
    errors, warnings = [], []
    if not lines:
        return errors, warnings
    for l in lines:
        link = url_for("planilla.profile", staff_id=l["staff_id"])
        for part in (l["warnings"] or "").split(" | "):
            if not part:
                continue
            target = errors if part.startswith(ERROR_PREFIXES) else warnings
            target.append({"text": f"{l['staff_name']}: {part}", "url": link})
        if not l["document_number"]:
            warnings.append({"text": f"{l['staff_name']}: sin número de documento (lo pide el PLAME).", "url": link})
        if l["pension_system"] == "AFP" and not l["cuspp"]:
            warnings.append({"text": f"{l['staff_name']}: AFP sin CUSPP (lo pide AFPnet).", "url": link})
        if l["net_pay"] > 0 and not (l["account_number"] or l["cci"]):
            warnings.append({"text": f"{l['staff_name']}: sin cuenta bancaria ni CCI, no entrará en el archivo de Telecrédito.", "url": link})
    # Personas marcadas en planilla que quedaron sin boleta (se marcaron después de calcular)
    have = {l["staff_id"] for l in lines}
    first = f"{p['period']}-01"
    for st in query_all(
        """SELECT * FROM staff WHERE in_payroll = 1 AND company_id = ?
           AND (status = 'ACTIVO' OR COALESCE(termination_date, '') >= ?) ORDER BY name""",
        (p["company_id"], first),
    ):
        if st["id"] not in have and worked_days(st, p["period"]) > 0:
            warnings.append({
                "text": f"{st['name']} está en planilla pero no tiene boleta: vuelve a calcular.",
                "url": url_for("planilla.profile", staff_id=st["id"]),
            })
    # Conceptos del mes que cambiaron después del último cálculo
    if p["status"] == "ABIERTO":
        items = query_all("SELECT * FROM payroll_items WHERE period_id = ?", (p["id"],))
        by = {}
        for i in items:
            acc = by.setdefault(i["staff_id"], [0.0, 0.0])
            acc[0 if i["kind"] == "INGRESO" else 1] += i["amount"]
        stale = set()
        for l in lines:
            ing, desc = by.get(l["staff_id"], [0.0, 0.0])
            if abs(ing - (l["other_income"] + l["other_income_nontaxable"])) > 0.005 or abs(desc - l["other_deduction"]) > 0.005:
                stale.add(l["staff_name"])
        for sid in set(by) - have:
            row = query_one("SELECT name FROM staff WHERE id = ?", (sid,))
            if row:
                stale.add(row["name"])
        if stale:
            warnings.append({
                "text": "Los conceptos del mes cambiaron desde el último cálculo (" + ", ".join(sorted(stale)) + "): recalcula la planilla.",
                "url": None,
            })
    earlier = query_one(
        "SELECT period FROM payroll_periods WHERE company_id = ? AND period < ? AND status = 'ABIERTO' ORDER BY period LIMIT 1",
        (p["company_id"], p["period"]),
    )
    if earlier and p["status"] == "ABIERTO":
        warnings.append({"text": f"El periodo {earlier['period']} sigue abierto: las planillas se cierran en orden.", "url": None})
    return errors, warnings


def period_panel(p, lines):
    """Todo lo que muestra el panel del periodo."""
    has_lines = bool(lines)
    files = []
    for kind, label in FILE_KINDS:
        state, text = export_status(p, kind, has_lines)
        files.append({"kind": kind, "label": label, "state": state, "text": text})
    t_state, t_text = transfer_status(p, has_lines)
    errors, warnings = period_alerts(p, lines)
    return {
        "kpis": {
            "net": r2(sum(l["net_pay"] for l in lines)),
            "cost": r2(sum(l["employer_cost"] for l in lines)),
            "fifth": r2(sum(l["fifth_deduction"] for l in lines)),
            "employer": r2(sum(l["essalud_employer"] for l in lines)),
            "people": len(lines),
        },
        "files": files,
        "transfer": {"state": t_state, "text": t_text},
        "errors": errors,
        "warnings": warnings,
    }
