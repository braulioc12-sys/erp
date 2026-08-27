import os
import uuid

from flask import Blueprint, Response, abort, current_app, flash, redirect, render_template, request, send_from_directory, url_for

from app.auth import permission_required, validate_csrf
from app.db import execute, query_all, query_one
from app.helpers import parse_date, parse_float, pretty_label, today_str
from app.reports import build_expenses_workbook
from app.routes.catalogos import get_catalog

bp = Blueprint("gastos", __name__, url_prefix="/gastos")

ALLOWED_RECEIPT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".pdf", ".webp", ".heic", ".heif"}

# Si el navegador no manda una extensión reconocible en el nombre del
# archivo (pasa a veces con fotos tomadas directo desde la cámara del
# celular), se usa el tipo MIME que sí manda el navegador para elegir una.
MIME_TO_EXTENSION = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "application/pdf": ".pdf",
}


def _receipts_dir():
    path = os.path.join(current_app.instance_path, "receipts")
    os.makedirs(path, exist_ok=True)
    return path


def _first_uploaded_file():
    """El formulario de gastos ofrece dos campos con el mismo nombre
    ("receipt"): uno con `capture="environment"` (abre la cámara directo en
    el celular) y otro para elegir un archivo ya existente (foto o PDF). El
    usuario solo llena uno; esto devuelve el primero que sí traiga un
    archivo."""
    for file_storage in request.files.getlist("receipt"):
        if file_storage and file_storage.filename:
            return file_storage
    return None


def _save_receipt(file_storage):
    """Guarda el comprobante adjunto (foto o PDF) y devuelve el nombre de
    archivo guardado, o None si no se envió nada válido. Nota: en hosting
    gratuito con disco efímero (ver README, sección Render) estos archivos
    se pierden al reiniciar/redesplegar, igual que la base de datos SQLite."""
    if not file_storage or not file_storage.filename:
        return None
    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in ALLOWED_RECEIPT_EXTENSIONS:
        ext = MIME_TO_EXTENSION.get((file_storage.mimetype or "").lower())
    if not ext:
        return None
    filename = f"{uuid.uuid4().hex}{ext}"
    file_storage.save(os.path.join(_receipts_dir(), filename))
    return filename


def budget_alerts():
    """Compara el gasto acumulado del mes en curso contra los presupuestos
    activos (por unidad o por tipo de gasto) y devuelve los que ya se
    excedieron o están a un 90% o más de su límite."""
    budgets = query_all("SELECT * FROM expense_budgets WHERE active = 1")
    alerts = []
    for b in budgets:
        if b["scope_type"] == "VEHICLE":
            vehicle = query_one("SELECT plate FROM vehicles WHERE id = ?", (b["scope_value"],))
            label = f"Unidad {vehicle['plate']}" if vehicle else f"Unidad #{b['scope_value']}"
            spent = query_one(
                """SELECT COALESCE(SUM(amount), 0) total FROM expenses
                   WHERE vehicle_id = ? AND strftime('%Y-%m', expense_date) = strftime('%Y-%m', 'now')""",
                (b["scope_value"],),
            )["total"]
        else:
            label = pretty_label(b["scope_value"])
            spent = query_one(
                """SELECT COALESCE(SUM(amount), 0) total FROM expenses
                   WHERE type = ? AND strftime('%Y-%m', expense_date) = strftime('%Y-%m', 'now')""",
                (b["scope_value"],),
            )["total"]
        if b["monthly_amount"] <= 0:
            continue
        ratio = spent / b["monthly_amount"]
        if ratio >= 0.9:
            alerts.append(
                {
                    "label": label,
                    "spent": spent,
                    "budget": b["monthly_amount"],
                    "ratio": ratio,
                    "over": ratio >= 1,
                }
            )
    return alerts


def _filtered_expenses(args):
    """Aplica los filtros de tipo y rango de fechas usados por la lista y la exportación."""
    type_filter = args.get("type", "")
    from_date = parse_date(args.get("from_date", ""))
    to_date = parse_date(args.get("to_date", ""))

    sql = """SELECT e.*, t.code as trip_code, v.plate as vehicle_plate
              FROM expenses e
              LEFT JOIN trips t ON t.id = e.trip_id
              LEFT JOIN vehicles v ON v.id = e.vehicle_id
              WHERE 1=1"""
    params = []
    if type_filter:
        sql += " AND e.type = ?"
        params.append(type_filter)
    if from_date:
        sql += " AND e.expense_date >= ?"
        params.append(from_date)
    if to_date:
        sql += " AND e.expense_date <= ?"
        params.append(to_date)
    sql += " ORDER BY e.expense_date DESC, e.id DESC"
    expenses = query_all(sql, params)
    return expenses, type_filter, from_date, to_date


@bp.route("")
@permission_required("gastos", "view")
def list_view():
    expenses, type_filter, from_date, to_date = _filtered_expenses(request.args)
    total = sum(e["amount"] for e in expenses)
    types = [c["name"] for c in get_catalog("expense_type")]
    return render_template(
        "gastos/list.html", expenses=expenses, types=types, type_filter=type_filter,
        from_date=from_date or "", to_date=to_date or "", total=total,
    )


@bp.route("/exportar")
@permission_required("gastos", "view")
def export_excel():
    from flask import current_app

    expenses, type_filter, from_date, to_date = _filtered_expenses(request.args)

    parts = []
    parts.append(f"Tipo: {pretty_label(type_filter)}" if type_filter else "Todos los tipos")
    if from_date or to_date:
        parts.append(f"Periodo: {from_date or 'inicio'} a {to_date or 'hoy'}")
    else:
        parts.append("Todas las fechas")
    filter_description = "  ·  ".join(parts)

    buffer = build_expenses_workbook(
        expenses,
        company_name=current_app.config["COMPANY_NAME"],
        filter_description=filter_description,
        known_type_order=[c["name"] for c in get_catalog("expense_type", only_active=False)],
    )
    filename = f"gastos_{today_str()}.xlsx"
    return Response(
        buffer.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.route("/nuevo", methods=["GET", "POST"])
@permission_required("gastos", "edit")
def new():
    trips = query_all("SELECT id, code FROM trips WHERE status != 'CANCELADO' ORDER BY scheduled_date DESC")
    vehicles = query_all("SELECT id, plate FROM vehicles ORDER BY plate")
    types = [c["name"] for c in get_catalog("expense_type")]
    preselected_trip = request.args.get("trip_id", type=int)

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        amount = parse_float(request.form.get("amount"))
        expense_date = parse_date(request.form.get("expense_date")) or today_str()
        expense_type = request.form.get("type")
        trip_id = request.form.get("trip_id") or None
        vehicle_id = request.form.get("vehicle_id") or None

        errors = []
        if expense_type not in types:
            errors.append("Selecciona un tipo de gasto válido.")
        if amount <= 0:
            errors.append("El monto debe ser mayor a cero.")
        if not trip_id and not vehicle_id:
            errors.append("Asocia el gasto a un viaje o a una unidad.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "gastos/form.html", expense=request.form, trips=trips, vehicles=vehicles, types=types,
            )

        receipt_filename = _save_receipt(_first_uploaded_file())

        execute(
            """INSERT INTO expenses (trip_id, vehicle_id, type, amount, expense_date, description, receipt_filename, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (trip_id, vehicle_id, expense_type, amount, expense_date, request.form.get("description", "").strip(), receipt_filename, None),
        )
        flash("Gasto registrado.", "success")
        if trip_id:
            return redirect(url_for("viajes.detail", trip_id=trip_id))
        return redirect(url_for("gastos.list_view"))

    return render_template(
        "gastos/form.html", expense=None, trips=trips, vehicles=vehicles, types=types,
        preselected_trip=preselected_trip, today=today_str(),
    )


@bp.route("/<int:expense_id>/eliminar", methods=["POST"])
@permission_required("gastos", "edit")
def delete(expense_id):
    if not validate_csrf():
        abort(400)
    execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
    flash("Gasto eliminado.", "success")
    return redirect(url_for("gastos.list_view"))


@bp.route("/<int:expense_id>/comprobante")
@permission_required("gastos", "view")
def receipt(expense_id):
    expense = query_one("SELECT receipt_filename FROM expenses WHERE id = ?", (expense_id,))
    if expense is None or not expense["receipt_filename"]:
        abort(404)
    return send_from_directory(_receipts_dir(), expense["receipt_filename"])


# --- Presupuestos de gasto ---

@bp.route("/presupuestos")
@permission_required("gastos", "edit")
def budgets_list():
    budgets = query_all("SELECT * FROM expense_budgets ORDER BY scope_type, scope_value")
    vehicles = query_all("SELECT id, plate FROM vehicles ORDER BY plate")
    types = [c["name"] for c in get_catalog("expense_type", only_active=False)]
    vehicle_plates = {v["id"]: v["plate"] for v in vehicles}
    return render_template(
        "gastos/budgets.html", budgets=budgets, vehicles=vehicles, types=types, vehicle_plates=vehicle_plates,
    )


@bp.route("/presupuestos/agregar", methods=["POST"])
@permission_required("gastos", "edit")
def budgets_add():
    if not validate_csrf():
        abort(400)
    scope_type = request.form.get("scope_type")
    scope_value = request.form.get("scope_value", "").strip()
    amount = parse_float(request.form.get("monthly_amount"), 0)
    if scope_type not in ("VEHICLE", "TYPE") or not scope_value or amount <= 0:
        flash("Completa unidad/tipo y un monto mensual válido.", "error")
        return redirect(url_for("gastos.budgets_list"))

    existing = query_one(
        "SELECT id FROM expense_budgets WHERE scope_type = ? AND scope_value = ?", (scope_type, scope_value)
    )
    if existing:
        execute(
            "UPDATE expense_budgets SET monthly_amount = ?, active = 1 WHERE id = ?",
            (amount, existing["id"]),
        )
        flash("Presupuesto actualizado.", "success")
    else:
        execute(
            "INSERT INTO expense_budgets (scope_type, scope_value, monthly_amount) VALUES (?, ?, ?)",
            (scope_type, scope_value, amount),
        )
        flash("Presupuesto agregado.", "success")
    return redirect(url_for("gastos.budgets_list"))


@bp.route("/presupuestos/<int:budget_id>/alternar", methods=["POST"])
@permission_required("gastos", "edit")
def budgets_toggle(budget_id):
    if not validate_csrf():
        abort(400)
    budget = query_one("SELECT * FROM expense_budgets WHERE id = ?", (budget_id,))
    if budget is None:
        abort(404)
    execute("UPDATE expense_budgets SET active = ? WHERE id = ?", (0 if budget["active"] else 1, budget_id))
    flash("Actualizado." if budget["active"] else "Reactivado.", "success")
    return redirect(url_for("gastos.budgets_list"))
