"""Catálogo de rutas frecuentes con un monto de viáticos predeterminado,
usado al confirmar el anticipo de gastos de viaje a un conductor."""
from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for

from app.audit import get_creator_info, log_activity
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
    # 7 sep, pedido de Braulio: antes el formulario de "agregar ruta" salía
    # siempre abierto arriba de la tabla; ahora es un botón aparte
    # ("+ Agregar nueva ruta", ver new()) y esta pantalla solo filtra/busca.
    q = request.args.get("q", "").strip()
    if q:
        # LOWER() en ambos lados (patch 0066) -- ver el comentario completo en
        # clientes.list_view().
        routes = query_all(
            "SELECT * FROM routes WHERE LOWER(origin) LIKE LOWER(?) OR LOWER(destination) LIKE LOWER(?) ORDER BY origin, destination",
            (f"%{q}%", f"%{q}%"),
        )
    else:
        routes = query_all("SELECT * FROM routes ORDER BY origin, destination")
    return render_template("rutas/list.html", routes=routes, q=q)


@bp.route("/nueva", methods=["GET"])
@permission_required("rutas", "edit")
def new():
    return render_template("rutas/new.html")


@bp.route("/agregar", methods=["POST"])
@permission_required("rutas", "edit")
def add():
    if not validate_csrf():
        abort(400)
    origin = request.form.get("origin", "").strip()
    destination = request.form.get("destination", "").strip()
    amount = parse_float(request.form.get("default_expense_amount"), 0)
    commission = parse_float(request.form.get("default_commission_amount"), 0)
    fuel_amount = parse_float(request.form.get("default_fuel_amount"), 0)
    if not origin or not destination:
        flash("Indica origen y destino.", "error")
        return redirect(url_for("rutas.new"))

    existing = query_one("SELECT id FROM routes WHERE origin = ? AND destination = ?", (origin, destination))
    if existing:
        execute(
            "UPDATE routes SET default_expense_amount = ?, default_commission_amount = ?, "
            "default_fuel_amount = ?, active = 1 WHERE id = ?",
            (amount, commission, fuel_amount, existing["id"]),
        )
        # 22 sep, registro de actividad (ver app/audit.py): este formulario
        # ("+ Agregar nueva ruta") en realidad actualiza si el origen/destino
        # ya existía -- se registra como EDITAR, no CREAR, para reflejar lo
        # que de verdad pasó en la base de datos.
        log_activity(
            "rutas", "EDITAR", f"Ruta {origin} → {destination} (actualizada vía 'Agregar nueva ruta')",
            entity_type="ruta", entity_id=existing["id"],
            entity_url=url_for("rutas.edit", route_id=existing["id"]),
        )
        flash("Ruta actualizada.", "success")
    else:
        route_id = execute(
            "INSERT INTO routes (origin, destination, default_expense_amount, "
            "default_commission_amount, default_fuel_amount) VALUES (?, ?, ?, ?, ?)",
            (origin, destination, amount, commission, fuel_amount),
        )
        log_activity(
            "rutas", "CREAR", f"Ruta {origin} → {destination}",
            entity_type="ruta", entity_id=route_id,
            entity_url=url_for("rutas.edit", route_id=route_id),
        )
        flash("Ruta agregada.", "success")
    return redirect(url_for("rutas.list_view"))


@bp.route("/<int:route_id>/editar", methods=["GET", "POST"])
@permission_required("rutas", "edit")
def edit(route_id):
    # 7 sep, pedido de Braulio ("quiero poder editar más adelante galones,
    # viáticos o comisión"): antes la única forma de "actualizar" una ruta
    # era volver a escribir EXACTAMENTE el mismo origen/destino en el
    # formulario de arriba (add()) — fácil de equivocarse (un espacio o una
    # mayúscula de más crea una ruta nueva en vez de actualizar la existente,
    # la causa más probable de las "rutas duplicadas" que reportó). Este
    # formulario edita por id, no por texto, así que no tiene ese riesgo.
    route = query_one("SELECT * FROM routes WHERE id = ?", (route_id,))
    if route is None:
        abort(404)
    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        origin = request.form.get("origin", "").strip()
        destination = request.form.get("destination", "").strip()
        amount = parse_float(request.form.get("default_expense_amount"), 0)
        commission = parse_float(request.form.get("default_commission_amount"), 0)
        fuel_amount = parse_float(request.form.get("default_fuel_amount"), 0)
        if not origin or not destination:
            flash("Indica origen y destino.", "error")
            return redirect(url_for("rutas.edit", route_id=route_id))
        clash = query_one(
            "SELECT id FROM routes WHERE origin = ? AND destination = ? AND id != ?",
            (origin, destination, route_id),
        )
        if clash:
            flash(f"Ya existe otra ruta con ese mismo origen y destino ({origin} → {destination}).", "error")
            return redirect(url_for("rutas.edit", route_id=route_id))
        execute(
            "UPDATE routes SET origin = ?, destination = ?, default_expense_amount = ?, "
            "default_commission_amount = ?, default_fuel_amount = ? WHERE id = ?",
            (origin, destination, amount, commission, fuel_amount, route_id),
        )
        log_activity(
            "rutas", "EDITAR", f"Ruta {origin} → {destination}",
            entity_type="ruta", entity_id=route_id,
            entity_url=url_for("rutas.edit", route_id=route_id),
        )
        flash("Ruta actualizada.", "success")
        return redirect(url_for("rutas.list_view"))
    # 22 sep, pedido de Braulio ("que usuario creo el viaje... etc"): quién y
    # cuándo se creó esta ruta, según activity_log (ver app/audit.py) -- None
    # para rutas de antes de que existiera este registro.
    creator = get_creator_info("ruta", route_id)
    return render_template("rutas/edit.html", route=route, creator=creator)


@bp.route("/<int:route_id>/alternar", methods=["POST"])
@permission_required("rutas", "edit")
def toggle(route_id):
    if not validate_csrf():
        abort(400)
    route = query_one("SELECT * FROM routes WHERE id = ?", (route_id,))
    if route is None:
        abort(404)
    new_active = 0 if route["active"] else 1
    execute("UPDATE routes SET active = ? WHERE id = ?", (new_active, route_id))
    log_activity(
        "rutas", "REACTIVAR" if new_active else "DESACTIVAR",
        f"Ruta {route['origin']} → {route['destination']}",
        entity_type="ruta", entity_id=route_id,
        entity_url=url_for("rutas.edit", route_id=route_id),
    )
    flash("Actualizada." if route["active"] else "Reactivada.", "success")
    return redirect(url_for("rutas.list_view"))


@bp.route("/<int:route_id>/eliminar", methods=["POST"])
@permission_required("rutas", "edit")
def delete(route_id):
    # 7 sep, pedido de Braulio ("veo rutas duplicadas que quiero borrar"):
    # antes solo existía Activar/Desactivar, nunca un borrado real. Los
    # viajes NO guardan una foreign key hacia routes (route_id solo se usa
    # para copiar origen/destino al crear el viaje, ver
    # _resolve_route_selection en app/routes/viajes.py), así que borrar una
    # ruta nunca puede romper un viaje ya creado. Lo único que sí referencia
    # routes.id de verdad es expense_advances.route_id — si una ruta ya se
    # usó para sugerir el monto de un anticipo de viáticos, se desactiva en
    # vez de borrarla (mismo criterio ya usado en Flota/Conductores).
    if not validate_csrf():
        abort(400)
    route = query_one("SELECT * FROM routes WHERE id = ?", (route_id,))
    if route is None:
        abort(404)
    has_history = query_one(
        "SELECT COUNT(*) n FROM expense_advances WHERE route_id = ?", (route_id,)
    )["n"]
    if has_history:
        execute("UPDATE routes SET active = 0 WHERE id = ?", (route_id,))
        log_activity(
            "rutas", "DESACTIVAR",
            f"Ruta {route['origin']} → {route['destination']} (se desactivó en vez de eliminar: "
            "ya tiene liquidaciones de viáticos)",
            entity_type="ruta", entity_id=route_id,
            entity_url=url_for("rutas.edit", route_id=route_id),
        )
        flash(
            "Esta ruta ya se usó en una liquidación de viáticos; se desactivó en vez de "
            "borrarla, para no perder ese historial.",
            "success",
        )
    else:
        execute("DELETE FROM routes WHERE id = ?", (route_id,))
        # 22 sep, registro de actividad: sin entity_url -- la ruta ya no
        # existe, así que rutas.edit para este id daría 404 (ver
        # app/routes/actividad.py, que ya maneja entity_url ausente).
        log_activity(
            "rutas", "ELIMINAR", f"Ruta {route['origin']} → {route['destination']}",
            entity_type="ruta", entity_id=route_id,
        )
        flash("Ruta eliminada.", "success")
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
        fuel_amount = row.get("default_fuel_amount") or 0
        existing = query_one("SELECT id FROM routes WHERE origin = ? AND destination = ?", (origin, destination))
        if existing:
            execute(
                "UPDATE routes SET default_expense_amount = ?, default_commission_amount = ?, "
                "default_fuel_amount = ?, active = 1 WHERE id = ?",
                (amount, commission, fuel_amount, existing["id"]),
            )
            updated += 1
        else:
            execute(
                "INSERT INTO routes (origin, destination, default_expense_amount, "
                "default_commission_amount, default_fuel_amount) VALUES (?, ?, ?, ?, ?)",
                (origin, destination, amount, commission, fuel_amount),
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
        # 22 sep, registro de actividad: una sola entrada para todo el lote
        # (no una por fila) -- son varios registros a la vez, igual que el
        # resto de cargas masivas del sistema.
        if result["created"] or result["updated"]:
            log_activity(
                "rutas", "CREAR" if result["created"] else "EDITAR",
                f"Importación masiva de rutas: {result['created']} creada(s), {result['updated']} actualizada(s)",
                entity_url=url_for("rutas.list_view"),
            )
        return render_template(
            "import_result.html", result=result,
            back_url=url_for("rutas.list_view"), retry_url=url_for("rutas.import_routes"),
        )
    return render_template(
        "import_form.html", title="Importar rutas", module_label="las rutas",
        template_url=url_for("rutas.import_template"), upload_url=url_for("rutas.import_routes"),
        back_url=url_for("rutas.list_view"), columns=ROUTE_COLUMNS,
    )
