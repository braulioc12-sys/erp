"""Vacaciones del personal (6 oct, pedido de Braulio: "algo similar a Buk",
fase 2 de 3 -- después del legajo; luego planillas).

Se calca lo que hace Buk en su cuenta (BRMS): un saldo de días por
trabajador, que crece solo con el tiempo desde su fecha de ingreso (la del
legajo, ver app/routes/legajo.py), y baja con las vacaciones que toma.

Cómo se calcula el saldo (verificado contra el saldo real de Buk del
6 oct 2026: ingreso 01/05/2025 -> 12.92 días): se cuentan los días entre la
fecha de ingreso y hoy con el método 30/360 (cada mes cuenta 30 días) y se
multiplica por (días por año ÷ 360). Con 30 días por año eso da 2.5 días por
mes. Al saldo se le restan las vacaciones GOZADAS y COMPRADAS ya APROBADAS y
se le suman/restan los AJUSTES (saldo inicial, correcciones). Las
solicitudes PENDIENTES no descuentan todavía, pero se muestran aparte.

Los días por año se configuran por persona (30 por defecto, régimen
general; en MYPE son 15). Si la persona ya cesó, el saldo se calcula hasta
su fecha de cese.
"""
from datetime import date, datetime

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from app.audit import log_activity
from app.auth import permission_required, validate_csrf
from app.db import execute, query_all, query_one
from app.helpers import now_str, parse_date, parse_float, today_str

bp = Blueprint("vacaciones", __name__, url_prefix="/vacaciones")

KIND_LABELS = {
    "GOZADAS": "Vacaciones gozadas",
    "COMPRADAS": "Vacaciones vendidas (pagadas)",
    "AJUSTE": "Ajuste de saldo",
}
STATUS_LABELS = {"PENDIENTE": "Pendiente", "APROBADA": "Aprobada", "RECHAZADA": "Rechazada"}

DEFAULT_DAYS_PER_YEAR = 30.0


def _d(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def days360(start, end):
    """Días entre dos fechas con el método 30/360 europeo (mes = 30 días)."""
    d1, d2 = min(start.day, 30), min(end.day, 30)
    return (end.year - start.year) * 360 + (end.month - start.month) * 30 + (d2 - d1)


def vacation_balance(staff, as_of=None):
    """Saldo de vacaciones de una persona (fila de `staff`). Devuelve un dict
    con accrued / taken / adjustments / pending / balance (todo en días,
    redondeado a 2 decimales), o None si la persona no tiene fecha de ingreso."""
    if not staff["hire_date"]:
        return None
    as_of_date = _d(as_of) if as_of else date.today()
    if staff["termination_date"]:
        as_of_date = min(as_of_date, _d(staff["termination_date"]))
    per_year = staff["vacation_days_per_year"] if staff["vacation_days_per_year"] is not None else DEFAULT_DAYS_PER_YEAR
    hire = _d(staff["hire_date"])
    accrued = max(days360(hire, as_of_date), 0) / 360 * per_year
    rows = query_all(
        "SELECT kind, status, days FROM staff_vacations WHERE staff_id = ?", (staff["id"],)
    )
    taken = sum(r["days"] for r in rows if r["status"] == "APROBADA" and r["kind"] in ("GOZADAS", "COMPRADAS"))
    adjustments = sum(r["days"] for r in rows if r["status"] == "APROBADA" and r["kind"] == "AJUSTE")
    pending = sum(r["days"] for r in rows if r["status"] == "PENDIENTE" and r["kind"] in ("GOZADAS", "COMPRADAS"))
    return {
        "accrued": round(accrued, 2), "taken": round(taken, 2), "adjustments": round(adjustments, 2),
        "pending": round(pending, 2), "per_year": per_year,
        "balance": round(accrued - taken + adjustments, 2),
    }


def _get_staff_or_404(staff_id):
    staff = query_one("SELECT * FROM staff WHERE id = ?", (staff_id,))
    if staff is None:
        abort(404)
    return staff


@bp.route("")
@permission_required("pagos_personal", "view")
def index():
    show_inactive = request.args.get("ver") == "inactivos"
    people = query_all(
        "SELECT * FROM staff WHERE status = ? ORDER BY name", ("INACTIVO" if show_inactive else "ACTIVO",)
    )
    rows = []
    for p in people:
        rows.append({"staff": p, "bal": vacation_balance(p)})
    pending = query_all(
        """SELECT v.*, s.name AS staff_name FROM staff_vacations v JOIN staff s ON s.id = v.staff_id
           WHERE v.status = 'PENDIENTE' ORDER BY v.start_date, v.id"""
    )
    return render_template(
        "vacaciones/index.html", rows=rows, pending=pending, show_inactive=show_inactive,
        kind_labels=KIND_LABELS,
    )


@bp.route("/<int:staff_id>")
@permission_required("pagos_personal", "view")
def detail(staff_id):
    staff = _get_staff_or_404(staff_id)
    entries = query_all(
        """SELECT v.*, u.name AS created_by_name, d.name AS decided_by_name
           FROM staff_vacations v LEFT JOIN users u ON u.id = v.created_by LEFT JOIN users d ON d.id = v.decided_by
           WHERE v.staff_id = ? ORDER BY COALESCE(v.start_date, v.created_at) DESC, v.id DESC""",
        (staff_id,),
    )
    return render_template(
        "vacaciones/detail.html", staff=staff, bal=vacation_balance(staff), entries=entries,
        kind_labels=KIND_LABELS, status_labels=STATUS_LABELS, today=today_str(),
    )


@bp.route("/<int:staff_id>/dias-por-anio", methods=["POST"])
@permission_required("pagos_personal", "edit")
def set_days_per_year(staff_id):
    if not validate_csrf():
        abort(400)
    staff = _get_staff_or_404(staff_id)
    value = parse_float(request.form.get("vacation_days_per_year"), None)
    if value is None or value <= 0 or value > 60:
        flash("Escribe los días de vacaciones por año (por ejemplo 30 o 15).", "error")
    else:
        execute("UPDATE staff SET vacation_days_per_year = ? WHERE id = ?", (value, staff_id))
        log_activity(
            "pagos_personal", "EDITAR", f"Vacaciones: {value:g} días por año para {staff['name']}",
            entity_type="empleado", entity_id=staff_id, entity_url=url_for("vacaciones.detail", staff_id=staff_id),
        )
        flash("Días de vacaciones por año actualizados.", "success")
    return redirect(url_for("vacaciones.detail", staff_id=staff_id))


@bp.route("/<int:staff_id>/registrar", methods=["POST"])
@permission_required("pagos_personal", "edit")
def add_entry(staff_id):
    """Registra vacaciones gozadas, vendidas o un ajuste de saldo."""
    if not validate_csrf():
        abort(400)
    staff = _get_staff_or_404(staff_id)
    form = request.form
    kind = form.get("kind", "")
    status = form.get("status", "APROBADA")
    if status not in ("PENDIENTE", "APROBADA"):
        status = "APROBADA"
    notes = form.get("notes", "").strip() or None
    start = parse_date(form.get("start_date"))
    end = parse_date(form.get("end_date"))
    days = None
    error = None
    if kind not in KIND_LABELS:
        error = "Elige qué quieres registrar."
    elif kind == "GOZADAS":
        if not start or not end:
            error = "Escribe la fecha de inicio y la de fin de las vacaciones."
        elif end < start:
            error = "La fecha de fin no puede ser anterior a la de inicio."
        else:
            days = float((_d(end) - _d(start)).days + 1)
            overlap = query_one(
                """SELECT id FROM staff_vacations WHERE staff_id = ? AND kind = 'GOZADAS' AND status != 'RECHAZADA'
                   AND start_date <= ? AND end_date >= ?""",
                (staff_id, end, start),
            )
            if overlap:
                error = "Ya hay vacaciones registradas que se cruzan con esas fechas."
    else:
        days = parse_float(form.get("days"), None)
        if days is None or days == 0 or (kind == "COMPRADAS" and days < 0):
            error = "Escribe la cantidad de días." if kind == "COMPRADAS" else "Escribe los días del ajuste (positivo suma, negativo resta)."
        if kind == "AJUSTE":
            status = "APROBADA"
        start = end = None
    if error:
        flash(error, "error")
        return redirect(url_for("vacaciones.detail", staff_id=staff_id))
    user = getattr(g, "user", None)
    execute(
        """INSERT INTO staff_vacations (staff_id, kind, start_date, end_date, days, status, notes, decided_by,
           decided_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            staff_id, kind, start, end, days, status, notes,
            user["id"] if user and status == "APROBADA" else None, now_str() if status == "APROBADA" else None,
            user["id"] if user else None,
        ),
    )
    detail_txt = f"{days:g} días" + (f" ({start} a {end})" if start else "")
    log_activity(
        "pagos_personal", "CREAR", f"Vacaciones — {KIND_LABELS[kind]}, {detail_txt}, {STATUS_LABELS[status].lower()}: {staff['name']}",
        entity_type="empleado", entity_id=staff_id, entity_url=url_for("vacaciones.detail", staff_id=staff_id),
    )
    msg = "Solicitud registrada, falta aprobarla." if status == "PENDIENTE" else "Registrado."
    flash(msg, "success")
    bal = vacation_balance(_get_staff_or_404(staff_id))
    if bal and bal["balance"] < 0 and status == "APROBADA":
        flash(f"Ojo: el saldo quedó en {bal['balance']:g} días (negativo).", "info")
    return redirect(url_for("vacaciones.detail", staff_id=staff_id))


@bp.route("/<int:staff_id>/<int:entry_id>/decidir", methods=["POST"])
@permission_required("pagos_personal", "edit")
def decide(staff_id, entry_id):
    """Aprueba o rechaza una solicitud PENDIENTE."""
    if not validate_csrf():
        abort(400)
    staff = _get_staff_or_404(staff_id)
    entry = query_one("SELECT * FROM staff_vacations WHERE id = ? AND staff_id = ?", (entry_id, staff_id))
    if entry is None:
        abort(404)
    decision = request.form.get("decision")
    if decision not in ("APROBADA", "RECHAZADA") or entry["status"] != "PENDIENTE":
        abort(400)
    user = getattr(g, "user", None)
    execute(
        "UPDATE staff_vacations SET status = ?, decided_by = ?, decided_at = ? WHERE id = ?",
        (decision, user["id"] if user else None, now_str(), entry_id),
    )
    log_activity(
        "pagos_personal", "ESTADO",
        f"Vacaciones ({entry['days']:g} días, {entry['start_date'] or 'sin fechas'}) de {staff['name']}: {STATUS_LABELS[decision].lower()}",
        entity_type="empleado", entity_id=staff_id, entity_url=url_for("vacaciones.detail", staff_id=staff_id),
    )
    flash("Solicitud aprobada." if decision == "APROBADA" else "Solicitud rechazada.", "success")
    next_url = request.form.get("next") or ""
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = url_for("vacaciones.detail", staff_id=staff_id)
    return redirect(next_url)


@bp.route("/<int:staff_id>/<int:entry_id>/eliminar", methods=["POST"])
@permission_required("pagos_personal", "edit")
def delete_entry(staff_id, entry_id):
    if not validate_csrf():
        abort(400)
    staff = _get_staff_or_404(staff_id)
    entry = query_one("SELECT * FROM staff_vacations WHERE id = ? AND staff_id = ?", (entry_id, staff_id))
    if entry is None:
        abort(404)
    execute("DELETE FROM staff_vacations WHERE id = ?", (entry_id,))
    log_activity(
        "pagos_personal", "ELIMINAR",
        f"Vacaciones — {KIND_LABELS.get(entry['kind'], entry['kind'])} de {entry['days']:g} días de {staff['name']}: eliminado",
        entity_type="empleado", entity_id=staff_id, entity_url=url_for("vacaciones.detail", staff_id=staff_id),
    )
    flash("Registro eliminado.", "success")
    return redirect(url_for("vacaciones.detail", staff_id=staff_id))
