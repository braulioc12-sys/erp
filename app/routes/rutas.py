"""Catálogo de rutas frecuentes con un monto de viáticos predeterminado,
usado al confirmar el anticipo de gastos de viaje a un conductor."""
from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for

from app.auth import permission_required, validate_csrf
from app.bulk_import import ROUTE_COLUMNS, ROUTE_EXAMPLE, XLSX_MIME, build_import_template, read_import_rows
from app.db import execute, query_all, query_one
from app.helpers import parse_float

bp = Blueprint("rutas", __name__, url_prefix="/rutas")


def find_route(origin, destination):
    return query_one(
        "SELECT * FROM routes WHERE active = 1 AND origin = ? AND destination = ?",
        (origin, destination),
    )


@bp.route("")
@permission_required("rutas", "view")
def list_view():
    routes = query_all("SELECT * FROM routes ORDER BY origin, destination")
    return render_template("rutas/list.html", routes=routes)


@bp.route("/agregar", methods=["POST"])
@permission_required("rutas", "edit")
def add():
    if not validate_csrf():
        abort(400)
    origin = request.form.get("origin", "").strip()
    destination = request.form.get("destination", "").strip()
    amount = parse_float(request.form.get("default_expense_amount"), 0)
    commission = parse_float(request.form.get("default_commission_amount"), 0)
    if not origin or not destination:
        flash("Indica origen y destino.", "error")
        return redirect(url_for("rutas.list_view"))

    existing = query_one("SELECT id FROM routes WHERE origin = ? AND destination = ?", (origin, destination))
    if existing:
        execute(
            "UPDATE routes SET default_expense_amount = ?, default_commission_amount = ?, active = 1 WHERE id = ?",
            (amount, commission, existing["id"]),
        )
        flash("Ruta actualizada.", "success")
    else:
        execute(
            "INSERT INTO routes (origin, destination, default_expense_amount, default_commission_amount) VALUES (?, ?, ?, ?)",
            (origin, destination, amount, commission),
        )
        flash("Ruta agregada.", "success")
    return redirect(url_for("rutas.list_view"))


@bp.route("/<int:route_id>/alternar", methods=["POST"])
@permission_required("rutas", "edit")
def toggle(route_id):
    if not validate_csrf():
        abort(400)
    route = query_one("SELECT * FROM routes WHERE id = ?", (route_id,))
    if route is None:
        abort(404)
    execute("UPDATE routes SET active = ? WHERE id = ?", (0 if route["active"] else 1, route_id))
    flash("Actualizada." if route["active"] else "Reactivada.", "success")
    return redirect(url_for("rutas.list_view"))


# --- Importación masiva desde Excel (30 ago, pedido de Braulio) ---

@bp.route("/importar/plantilla")
@permission_required("rutas", "edit")
def import_template():
    buffer = build_import_template("Rutas y viáticos", ROUTE_COLUMNS, ROUTE_EXAMPLE)
    return Response(
        buffer.getvalue(),
        mimetype=XLSX_MIME,
        headers={"Content-Disposition": 'attachment; filename="plantilla_rutas.xlsx"'},
    )


def _apply_route_import(rows, example_skips):
    created, updated, errors = 0, 0, []
    skipped = [
        {"row": r, "message": "Fila de ejemplo de la plantilla; se omitió automáticamente."}
        for r in example_skips
    ]
    seen = set()
    for row in rows:
        n = row["_row_number"]
        for warn in row["_warnings"]:
            errors.append({"row": n, "message": warn})
        origin = (row.get("origin") or "").strip()
        destination = (row.get("destination") or "").strip()
        if not origin or not destination:
            errors.append({"row": n, "message": "Falta origen o destino; la fila no se importó."})
            continue
        key = (origin.lower(), destination.lower())
        if key in seen:
            skipped.append({"row": n, "message": f"{origin} → {destination} está repetida dentro del archivo; ya se había importado antes."})
            continue
        seen.add(key)
        amount = row.get("default_expense_amount") or 0
        commission = row.get("default_commission_amount") or 0
        existing = query_one("SELECT id FROM routes WHERE origin = ? AND destination = ?", (origin, destination))
        if existing:
            execute(
                "UPDATE routes SET default_expense_amount = ?, default_commission_amount = ?, active = 1 WHERE id = ?",
                (amount, commission, existing["id"]),
            )
            updated += 1
        else:
            execute(
                "INSERT INTO routes (origin, destination, default_expense_amount, default_commission_amount) VALUES (?, ?, ?, ?)",
                (origin, destination, amount, commission),
            )
            created += 1
    return {"created": created, "updated": updated, "skipped": skipped, "errors": errors}


@bp.route("/importar", methods=["GET", "POST"])
@permission_required("rutas", "edit")
def import_routes():
    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        rows, file_error, example_skips = read_import_rows(request.files.get("file"), ROUTE_COLUMNS, ROUTE_EXAMPLE)
        if file_error:
            flash(file_error, "error")
            return redirect(url_for("rutas.import_routes"))
        result = _apply_route_import(rows, example_skips)
        return render_template(
            "import_result.html", result=result,
            back_url=url_for("rutas.list_view"), retry_url=url_for("rutas.import_routes"),
        )
    return render_template(
        "import_form.html", title="Importar rutas", module_label="las rutas",
        template_url=url_for("rutas.import_template"), upload_url=url_for("rutas.import_routes"),
        back_url=url_for("rutas.list_view"), columns=ROUTE_COLUMNS,
    )
