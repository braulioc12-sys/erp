from datetime import datetime

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app.auth import permission_required, validate_csrf
from app.db import execute, get_db, get_setting, query_all, query_one
from app.helpers import parse_date, parse_float, today_str
from app.routes.inventarios import get_catalog_items
from app.seed_data import DEFAULT_JOB_TYPES, MECHANIC_TYPES, labor_cost_setting_key

bp = Blueprint("mantenimiento", __name__, url_prefix="/mantenimiento")

# Umbral de kilómetros para mostrar la alerta de "mantenimiento próximo" en el panel.
KM_ALERT_THRESHOLD = 1000


def _order_status(jobs):
    """Estado general de una orden de mantenimiento, calculado a partir del
    estado de cada trabajo (maintenance_record_jobs.status): SIN_TRABAJOS si
    no se marcó ningún trabajo al crearla, TERMINADA si todos están
    TERMINADO, PENDIENTE si ninguno lo está, EN_PROCESO en cualquier
    combinación intermedia."""
    if not jobs:
        return "SIN_TRABAJOS"
    done = sum(1 for j in jobs if j["status"] == "TERMINADO")
    if done == 0:
        return "PENDIENTE"
    if done == len(jobs):
        return "TERMINADA"
    return "EN_PROCESO"


ORDER_STATUS_LABELS = {
    "SIN_TRABAJOS": "Sin trabajos",
    "PENDIENTE": "Pendiente",
    "EN_PROCESO": "En proceso",
    "TERMINADA": "Terminada",
}


def _mechanic_type_from_form(job_id):
    mechanic_type = request.form.get(f"mechanic_type_{job_id}", "").strip()
    return mechanic_type if mechanic_type in MECHANIC_TYPES else "Otros"


def _mechanic_count_from_form(job_id):
    count = parse_float(request.form.get(f"mechanic_count_{job_id}"), 1) or 1
    return max(1, int(count))


def _insert_selected_jobs(db, record_id, selected_jobs):
    """Inserta filas nuevas en maintenance_record_jobs para los trabajos
    marcados, leyendo el tipo y la cantidad de mecánicos de cada uno desde
    el formulario (campos mechanic_type_<id> / mechanic_count_<id>).
    INSERT OR IGNORE por si alguno ya estaba en la orden (evita un error de
    llave primaria duplicada, ej. dos envíos del mismo formulario). Devuelve
    la suma de minutos estimados efectivamente agregados."""
    total_minutes = 0
    for j in selected_jobs:
        mechanic_type = _mechanic_type_from_form(j["id"])
        mechanic_count = _mechanic_count_from_form(j["id"])
        cur = db.execute(
            """INSERT OR IGNORE INTO maintenance_record_jobs
               (maintenance_record_id, job_type_id, job_name, estimated_minutes, mechanic_type, mechanic_count)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (record_id, j["id"], j["name"], j["estimated_minutes"], mechanic_type, mechanic_count),
        )
        if cur.rowcount:
            total_minutes += j["estimated_minutes"]
    return total_minutes


def _insert_selected_materials(db, record_id, selected_materials):
    """Inserta filas nuevas en maintenance_record_materials para los
    materiales/repuestos marcados, leyendo la cantidad de cada uno desde el
    formulario (campo material_qty_<id>). Ignora los que quedaron con
    cantidad 0 o vacía. Descuenta la cantidad usada del stock del repuesto
    en Inventarios (inventory_items.stock_quantity) — se permite que quede
    en negativo (pedido explícito de Braulio: no bloquear, solo avisar);
    devuelve la lista de avisos de stock negativo para que el llamador los
    muestre con flash()."""
    warnings = []
    for m in selected_materials:
        qty = parse_float(request.form.get(f"material_qty_{m['id']}"), 0) or 0
        if qty <= 0:
            continue
        db.execute(
            """INSERT INTO maintenance_record_materials
               (maintenance_record_id, material_id, material_name, unit_cost, quantity)
               VALUES (?, ?, ?, ?, ?)""",
            (record_id, m["id"], m["name"], m["unit_cost"], qty),
        )
        db.execute(
            "UPDATE inventory_items SET stock_quantity = stock_quantity - ? WHERE id = ?",
            (qty, m["id"]),
        )
        new_stock = db.execute(
            "SELECT stock_quantity FROM inventory_items WHERE id = ?", (m["id"],)
        ).fetchone()
        if new_stock is not None and new_stock["stock_quantity"] < 0:
            warnings.append(
                f'Stock de "{m["name"]}" quedó en {new_stock["stock_quantity"]:g} (negativo) — revisa Inventarios.'
            )
    return warnings


@bp.route("")
@permission_required("mantenimiento", "view")
def list_view():
    vehicle_id = request.args.get("vehicle_id", type=int)
    sql = """SELECT m.*, v.plate as vehicle_plate FROM maintenance_records m
              JOIN vehicles v ON v.id = m.vehicle_id WHERE 1=1"""
    params = []
    if vehicle_id:
        sql += " AND m.vehicle_id = ?"
        params.append(vehicle_id)
    sql += " ORDER BY m.maintenance_date DESC"
    records = query_all(sql, params)

    jobs_by_record = {}
    status_by_record = {}
    if records:
        ids = [r["id"] for r in records]
        placeholders = ",".join("?" * len(ids))
        rows = query_all(
            f"SELECT * FROM maintenance_record_jobs WHERE maintenance_record_id IN ({placeholders})", ids
        )
        jobs_grouped = {}
        for row in rows:
            jobs_by_record.setdefault(row["maintenance_record_id"], []).append(row["job_name"])
            jobs_grouped.setdefault(row["maintenance_record_id"], []).append(row)
        for r in records:
            status_by_record[r["id"]] = _order_status(jobs_grouped.get(r["id"], []))

    filtered_vehicle = (
        query_one("SELECT id, plate, current_km, current_km_updated_at FROM vehicles WHERE id = ?", (vehicle_id,))
        if vehicle_id else None
    )
    return render_template(
        "mantenimiento/list.html", records=records, jobs_by_record=jobs_by_record,
        status_by_record=status_by_record, order_status_labels=ORDER_STATUS_LABELS,
        vehicle_id=vehicle_id, filtered_vehicle=filtered_vehicle,
    )


@bp.route("/nuevo", methods=["GET", "POST"])
@permission_required("mantenimiento", "edit")
def new():
    vehicles = query_all("SELECT id, plate, current_km FROM vehicles ORDER BY plate")
    job_types = get_catalog_jobs()
    materials = get_catalog_items()
    labor_costs = {t: get_setting(labor_cost_setting_key(t), "0") for t in MECHANIC_TYPES}

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        vehicle_id = request.form.get("vehicle_id")
        maintenance_date = parse_date(request.form.get("maintenance_date")) or today_str()
        odometer_km = parse_float(request.form.get("odometer_km"), None)
        job_ids = [int(j) for j in request.form.getlist("job_type_ids")]
        material_ids = [int(m) for m in request.form.getlist("material_ids")]

        errors = []
        if not vehicle_id:
            errors.append("Selecciona una unidad.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "mantenimiento/form.html", record=request.form, vehicles=vehicles,
                job_types=job_types, materials=materials, labor_costs=labor_costs,
                mechanic_types=MECHANIC_TYPES,
            )

        selected_jobs = [j for j in job_types if j["id"] in job_ids]
        selected_materials = [m for m in materials if m["id"] in material_ids]
        estimated_minutes = sum(j["estimated_minutes"] for j in selected_jobs) or None
        # Ya no se pide un "Concepto" aparte (retirado el 28 ago — los
        # trabajos marcados son los que clasifican la orden). `type` sigue
        # existiendo en el esquema (columna NOT NULL, usada para mostrar la
        # orden en el listado), así que se completa solo con los nombres de
        # los trabajos marcados, o un texto genérico si no se marcó ninguno.
        record_type = ", ".join(j["name"] for j in selected_jobs) if selected_jobs else "Mantenimiento general"

        record_id = execute(
            """INSERT INTO maintenance_records (vehicle_id, type, maintenance_date, cost, description,
               odometer_km, next_due_date, next_due_km, estimated_minutes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                vehicle_id,
                record_type,
                maintenance_date,
                parse_float(request.form.get("cost")),
                request.form.get("description", "").strip(),
                odometer_km,
                parse_date(request.form.get("next_due_date")),
                parse_float(request.form.get("next_due_km"), None),
                estimated_minutes,
            ),
        )

        if selected_jobs or selected_materials:
            db = get_db()
            _insert_selected_jobs(db, record_id, selected_jobs)
            stock_warnings = _insert_selected_materials(db, record_id, selected_materials)
            db.commit()
            for w in stock_warnings:
                flash(w, "error")

        # Si se indicó el kilometraje al momento del mantenimiento, lo usamos
        # para actualizar el kilometraje actual de la unidad (evita tener que
        # registrarlo dos veces).
        if odometer_km is not None:
            execute(
                "UPDATE vehicles SET current_km = ?, current_km_updated_at = ? WHERE id = ?",
                (odometer_km, maintenance_date, vehicle_id),
            )

        if request.form.get("mark_in_maintenance"):
            execute("UPDATE vehicles SET status = 'MANTENIMIENTO' WHERE id = ?", (vehicle_id,))

        flash("Mantenimiento registrado.", "success")
        return redirect(url_for("mantenimiento.list_view"))

    return render_template(
        "mantenimiento/form.html", record=None, vehicles=vehicles,
        job_types=job_types, materials=materials, labor_costs=labor_costs,
        mechanic_types=MECHANIC_TYPES, today=today_str(),
    )


@bp.route("/<int:record_id>/eliminar", methods=["POST"])
@permission_required("mantenimiento", "edit")
def delete(record_id):
    if not validate_csrf():
        abort(400)
    execute("DELETE FROM maintenance_record_jobs WHERE maintenance_record_id = ?", (record_id,))
    execute("DELETE FROM maintenance_record_materials WHERE maintenance_record_id = ?", (record_id,))
    execute("DELETE FROM maintenance_records WHERE id = ?", (record_id,))
    flash("Registro de mantenimiento eliminado.", "success")
    return redirect(url_for("mantenimiento.list_view"))


# --- Detalle de una orden: marcar trabajos terminados/pendientes y asignar mecánico ---

@bp.route("/<int:record_id>")
@permission_required("mantenimiento", "view")
def detail(record_id):
    record = query_one(
        """SELECT m.*, v.plate as vehicle_plate FROM maintenance_records m
           JOIN vehicles v ON v.id = m.vehicle_id WHERE m.id = ?""",
        (record_id,),
    )
    if record is None:
        abort(404)
    jobs = query_all(
        "SELECT * FROM maintenance_record_jobs WHERE maintenance_record_id = ? ORDER BY job_name",
        (record_id,),
    )
    materials = query_all(
        "SELECT * FROM maintenance_record_materials WHERE maintenance_record_id = ? ORDER BY id",
        (record_id,),
    )
    mechanics = get_catalog_mechanics()
    used_job_names = {j["job_name"] for j in jobs}
    available_job_types = [j for j in get_catalog_jobs() if j["name"] not in used_job_names]
    available_materials = get_catalog_items()
    labor_costs = {t: get_setting(labor_cost_setting_key(t), "0") for t in MECHANIC_TYPES}
    materials_total = sum((mtl["unit_cost"] or 0) * (mtl["quantity"] or 0) for mtl in materials)
    return render_template(
        "mantenimiento/detail.html", record=record, jobs=jobs, materials=materials, mechanics=mechanics,
        order_status=_order_status(jobs), order_status_labels=ORDER_STATUS_LABELS,
        mechanic_types=MECHANIC_TYPES, available_job_types=available_job_types,
        available_materials=available_materials, labor_costs=labor_costs, materials_total=materials_total,
    )


@bp.route("/<int:record_id>/agregar", methods=["POST"])
@permission_required("mantenimiento", "edit")
def add_more(record_id):
    """Agrega trabajos y/o materiales adicionales a una orden ya creada —
    para lo que se descubre sobre la marcha durante el mantenimiento
    (pedido de Braulio, 28 ago — 4ª ronda). Suma el costo indicado y los
    minutos de los trabajos nuevos al total ya guardado de la orden (no
    reemplaza lo que ya había)."""
    if not validate_csrf():
        abort(400)
    record = query_one("SELECT * FROM maintenance_records WHERE id = ?", (record_id,))
    if record is None:
        abort(404)

    job_types = get_catalog_jobs()
    job_ids = [int(j) for j in request.form.getlist("job_type_ids")]
    selected_jobs = [j for j in job_types if j["id"] in job_ids]

    materials_catalog = get_catalog_items()
    material_ids = [int(m) for m in request.form.getlist("material_ids")]
    selected_materials = [m for m in materials_catalog if m["id"] in material_ids]

    if not selected_jobs and not selected_materials:
        flash("Selecciona al menos un trabajo o material para agregar.", "error")
        return redirect(url_for("mantenimiento.detail", record_id=record_id))

    db = get_db()
    added_minutes = _insert_selected_jobs(db, record_id, selected_jobs)
    stock_warnings = _insert_selected_materials(db, record_id, selected_materials)

    added_cost = parse_float(request.form.get("added_cost"), 0) or 0
    db.execute(
        "UPDATE maintenance_records SET cost = ?, estimated_minutes = ? WHERE id = ?",
        ((record["cost"] or 0) + added_cost, (record["estimated_minutes"] or 0) + added_minutes, record_id),
    )
    db.commit()
    flash("Se agregaron trabajos/materiales a la orden.", "success")
    for w in stock_warnings:
        flash(w, "error")
    return redirect(url_for("mantenimiento.detail", record_id=record_id))


@bp.route("/<int:record_id>/trabajos/cantidad-mecanicos", methods=["POST"])
@permission_required("mantenimiento", "edit")
def job_set_mechanic_count(record_id):
    if not validate_csrf():
        abort(400)
    job_name = request.form.get("job_name", "")
    count = parse_float(request.form.get("mechanic_count"), 1) or 1
    count = max(1, int(count))
    job = query_one(
        "SELECT * FROM maintenance_record_jobs WHERE maintenance_record_id = ? AND job_name = ?",
        (record_id, job_name),
    )
    if job is None:
        abort(404)
    execute(
        """UPDATE maintenance_record_jobs SET mechanic_count = ?
           WHERE maintenance_record_id = ? AND job_name = ?""",
        (count, record_id, job_name),
    )
    flash(f'Cantidad de mecánicos de "{job_name}" actualizada a {count}.', "success")
    return redirect(url_for("mantenimiento.detail", record_id=record_id))


@bp.route("/<int:record_id>/trabajos/estado", methods=["POST"])
@permission_required("mantenimiento", "edit")
def job_set_status(record_id):
    if not validate_csrf():
        abort(400)
    job_name = request.form.get("job_name", "")
    job = query_one(
        "SELECT * FROM maintenance_record_jobs WHERE maintenance_record_id = ? AND job_name = ?",
        (record_id, job_name),
    )
    if job is None:
        abort(404)
    new_status = "PENDIENTE" if job["status"] == "TERMINADO" else "TERMINADO"
    completed_at = datetime.now().strftime("%Y-%m-%d %H:%M") if new_status == "TERMINADO" else None
    execute(
        """UPDATE maintenance_record_jobs SET status = ?, completed_at = ?
           WHERE maintenance_record_id = ? AND job_name = ?""",
        (new_status, completed_at, record_id, job_name),
    )
    flash(
        f'"{job_name}" marcado como {"terminado" if new_status == "TERMINADO" else "pendiente"}.',
        "success",
    )
    return redirect(url_for("mantenimiento.detail", record_id=record_id))


@bp.route("/<int:record_id>/trabajos/mecanico", methods=["POST"])
@permission_required("mantenimiento", "edit")
def job_assign_mechanic(record_id):
    if not validate_csrf():
        abort(400)
    job_name = request.form.get("job_name", "")
    mechanic_id = request.form.get("mechanic_id", "").strip()
    job = query_one(
        "SELECT * FROM maintenance_record_jobs WHERE maintenance_record_id = ? AND job_name = ?",
        (record_id, job_name),
    )
    if job is None:
        abort(404)
    if not mechanic_id:
        execute(
            """UPDATE maintenance_record_jobs SET mechanic_id = NULL, mechanic_name = NULL
               WHERE maintenance_record_id = ? AND job_name = ?""",
            (record_id, job_name),
        )
        flash(f'Se quitó el mecánico asignado a "{job_name}".', "success")
    else:
        mechanic = query_one("SELECT * FROM mechanics WHERE id = ?", (mechanic_id,))
        if mechanic is None:
            abort(404)
        execute(
            """UPDATE maintenance_record_jobs SET mechanic_id = ?, mechanic_name = ?
               WHERE maintenance_record_id = ? AND job_name = ?""",
            (mechanic["id"], mechanic["name"], record_id, job_name),
        )
        flash(f'"{mechanic["name"]}" asignado a "{job_name}".', "success")
    return redirect(url_for("mantenimiento.detail", record_id=record_id))


@bp.route("/<int:record_id>/trabajos/tipo-mecanico", methods=["POST"])
@permission_required("mantenimiento", "edit")
def job_set_mechanic_type(record_id):
    """Cambia el tipo de mecánico (Senior/Junior/Practicante/Otros) de un
    trabajo dentro de la orden — se elige al crear la orden, pero se puede
    corregir aquí después. Es independiente de a qué persona se asigne
    (mechanic_id/mechanic_name): este campo es el que determina el costo
    de mano de obra sugerido de ese trabajo."""
    if not validate_csrf():
        abort(400)
    job_name = request.form.get("job_name", "")
    mechanic_type = request.form.get("mechanic_type", "").strip()
    if mechanic_type not in MECHANIC_TYPES:
        abort(400)
    job = query_one(
        "SELECT * FROM maintenance_record_jobs WHERE maintenance_record_id = ? AND job_name = ?",
        (record_id, job_name),
    )
    if job is None:
        abort(404)
    execute(
        """UPDATE maintenance_record_jobs SET mechanic_type = ?
           WHERE maintenance_record_id = ? AND job_name = ?""",
        (mechanic_type, record_id, job_name),
    )
    flash(f'Tipo de mecánico de "{job_name}" actualizado a {mechanic_type}.', "success")
    return redirect(url_for("mantenimiento.detail", record_id=record_id))


# --- Trabajos de mantenimiento (catálogo con tiempo estimado) ---

def get_catalog_jobs(only_active=True):
    sql = "SELECT * FROM maintenance_job_types WHERE 1=1"
    if only_active:
        sql += " AND active = 1"
    sql += " ORDER BY sort_order, name"
    return query_all(sql)


@bp.route("/trabajos")
@permission_required("mantenimiento", "view")
def jobs_list():
    jobs = query_all("SELECT * FROM maintenance_job_types ORDER BY sort_order, name")
    return render_template("mantenimiento/jobs.html", jobs=jobs, default_job_types=DEFAULT_JOB_TYPES)


@bp.route("/trabajos/agregar", methods=["POST"])
@permission_required("mantenimiento", "edit")
def jobs_add():
    if not validate_csrf():
        abort(400)
    name = request.form.get("name", "").strip()
    minutes = parse_float(request.form.get("estimated_minutes"), 0)
    if not name:
        flash("Escribe el nombre del trabajo.", "error")
        return redirect(url_for("mantenimiento.jobs_list"))

    existing = query_one("SELECT id, active FROM maintenance_job_types WHERE name = ?", (name,))
    if existing:
        if existing["active"]:
            flash("Ese trabajo ya existe.", "error")
        else:
            execute(
                "UPDATE maintenance_job_types SET active = 1, estimated_minutes = ? WHERE id = ?",
                (int(minutes), existing["id"]),
            )
            flash(f'"{name}" reactivado.', "success")
    else:
        max_order = query_one("SELECT COALESCE(MAX(sort_order), -1) m FROM maintenance_job_types")["m"]
        execute(
            "INSERT INTO maintenance_job_types (name, estimated_minutes, sort_order) VALUES (?, ?, ?)",
            (name, int(minutes), max_order + 1),
        )
        flash(f'"{name}" agregado.', "success")
    return redirect(url_for("mantenimiento.jobs_list"))


@bp.route("/trabajos/reemplazar-catalogo", methods=["POST"])
@permission_required("mantenimiento", "edit")
def jobs_replace_catalog():
    """Borra todos los trabajos actuales y carga la lista de DEFAULT_JOB_TYPES
    (app/seed_data.py) — usado para reemplazar el catálogo completo por uno
    nuevo (ej. el Excel de actividades de taller que entregó Braulio el 28
    ago). El historial de mantenimientos ya guarda su propia copia del
    nombre y los minutos de cada trabajo (maintenance_record_jobs.job_name /
    estimated_minutes), así que desvincular esas filas del catálogo (job_type_id
    = NULL) no borra ni cambia nada de lo ya registrado — solo deja de
    apuntar a una fila del catálogo que ya no existe."""
    if not validate_csrf():
        abort(400)
    db = get_db()
    db.execute("UPDATE maintenance_record_jobs SET job_type_id = NULL")
    db.execute("DELETE FROM maintenance_job_types")
    for order, (name, minutes) in enumerate(DEFAULT_JOB_TYPES):
        db.execute(
            "INSERT INTO maintenance_job_types (name, estimated_minutes, sort_order) VALUES (?, ?, ?)",
            (name, minutes, order),
        )
    db.commit()
    flash(f"Catálogo de trabajos reemplazado: {len(DEFAULT_JOB_TYPES)} trabajos cargados.", "success")
    return redirect(url_for("mantenimiento.jobs_list"))


@bp.route("/trabajos/<int:job_id>/alternar", methods=["POST"])
@permission_required("mantenimiento", "edit")
def jobs_toggle(job_id):
    if not validate_csrf():
        abort(400)
    job = query_one("SELECT * FROM maintenance_job_types WHERE id = ?", (job_id,))
    if job is None:
        abort(404)
    execute("UPDATE maintenance_job_types SET active = ? WHERE id = ?", (0 if job["active"] else 1, job_id))
    flash("Actualizado." if job["active"] else "Reactivado.", "success")
    return redirect(url_for("mantenimiento.jobs_list"))


# --- Mecánicos (catálogo para asignar quién trabaja cada trabajo) ---

def get_catalog_mechanics(only_active=True):
    sql = "SELECT * FROM mechanics WHERE 1=1"
    if only_active:
        sql += " AND active = 1"
    sql += " ORDER BY sort_order, name"
    return query_all(sql)


@bp.route("/mecanicos")
@permission_required("mantenimiento", "view")
def mechanics_list():
    mechanics = query_all("SELECT * FROM mechanics ORDER BY sort_order, name")
    return render_template("mantenimiento/mechanics.html", mechanics=mechanics, mechanic_types=MECHANIC_TYPES)


@bp.route("/mecanicos/agregar", methods=["POST"])
@permission_required("mantenimiento", "edit")
def mechanics_add():
    if not validate_csrf():
        abort(400)
    name = request.form.get("name", "").strip()
    mechanic_type = request.form.get("mechanic_type", "").strip()
    if not name:
        flash("Escribe el nombre del mecánico.", "error")
        return redirect(url_for("mantenimiento.mechanics_list"))
    if mechanic_type not in MECHANIC_TYPES:
        flash("Selecciona un tipo de mecánico válido.", "error")
        return redirect(url_for("mantenimiento.mechanics_list"))

    existing = query_one("SELECT id, active FROM mechanics WHERE name = ?", (name,))
    if existing:
        if existing["active"]:
            flash("Ese mecánico ya existe.", "error")
        else:
            execute(
                "UPDATE mechanics SET active = 1, mechanic_type = ? WHERE id = ?",
                (mechanic_type, existing["id"]),
            )
            flash(f'"{name}" reactivado.', "success")
    else:
        max_order = query_one("SELECT COALESCE(MAX(sort_order), -1) m FROM mechanics")["m"]
        execute(
            "INSERT INTO mechanics (name, mechanic_type, sort_order) VALUES (?, ?, ?)",
            (name, mechanic_type, max_order + 1),
        )
        flash(f'"{name}" agregado.', "success")
    return redirect(url_for("mantenimiento.mechanics_list"))


@bp.route("/mecanicos/<int:mechanic_id>/tipo", methods=["POST"])
@permission_required("mantenimiento", "edit")
def mechanics_set_type(mechanic_id):
    if not validate_csrf():
        abort(400)
    mechanic_type = request.form.get("mechanic_type", "").strip()
    if mechanic_type not in MECHANIC_TYPES:
        abort(400)
    mechanic = query_one("SELECT * FROM mechanics WHERE id = ?", (mechanic_id,))
    if mechanic is None:
        abort(404)
    execute("UPDATE mechanics SET mechanic_type = ? WHERE id = ?", (mechanic_type, mechanic_id))
    flash(f'Tipo de "{mechanic["name"]}" actualizado a {mechanic_type}.', "success")
    return redirect(url_for("mantenimiento.mechanics_list"))


@bp.route("/mecanicos/<int:mechanic_id>/alternar", methods=["POST"])
@permission_required("mantenimiento", "edit")
def mechanics_toggle(mechanic_id):
    if not validate_csrf():
        abort(400)
    mechanic = query_one("SELECT * FROM mechanics WHERE id = ?", (mechanic_id,))
    if mechanic is None:
        abort(404)
    execute("UPDATE mechanics SET active = ? WHERE id = ?", (0 if mechanic["active"] else 1, mechanic_id))
    flash("Actualizado." if mechanic["active"] else "Reactivado.", "success")
    return redirect(url_for("mantenimiento.mechanics_list"))


# --- Historial y costos por unidad ---

@bp.route("/por-unidad")
@permission_required("mantenimiento", "view")
def by_vehicle():
    summary = query_all(
        """SELECT v.id, v.plate, v.current_km, v.current_km_updated_at, COUNT(m.id) as n_records,
                  COALESCE(SUM(m.cost), 0) as total_cost,
                  MAX(m.maintenance_date) as last_date
           FROM vehicles v
           LEFT JOIN maintenance_records m ON m.vehicle_id = v.id
           GROUP BY v.id
           ORDER BY v.plate"""
    )
    return render_template("mantenimiento/by_vehicle.html", summary=summary)


@bp.route("/unidad/<int:vehicle_id>/kilometraje", methods=["POST"])
@permission_required("mantenimiento", "edit")
def update_vehicle_km(vehicle_id):
    """2 sep, pedido de Braulio: un recuadro para corregir a mano el
    kilometraje de una unidad directamente desde Mantenimiento ("dentro de
    taller"), por si el GPS dejó de transmitir y `vehicles.current_km` (que
    normalmente se actualiza solo cada 2 minutos vía Frotcom — ver
    perform_frotcom_sync en integraciones.py) se quedó desactualizado. Mismo
    campo que ya se puede editar en Flota → Editar unidad; esto solo agrega
    un atajo más rápido, sin salir de Mantenimiento, para el caso de
    emergencia."""
    if not validate_csrf():
        abort(400)
    vehicle = query_one("SELECT id FROM vehicles WHERE id = ?", (vehicle_id,))
    if vehicle is None:
        abort(404)
    new_km = parse_float(request.form.get("current_km"), None)
    if new_km is None:
        flash("Ingresa un kilometraje válido.", "error")
    else:
        execute(
            "UPDATE vehicles SET current_km = ?, current_km_updated_at = ? WHERE id = ?",
            (new_km, today_str(), vehicle_id),
        )
        flash("Kilometraje actualizado.", "success")
    next_url = request.form.get("next") or url_for("mantenimiento.by_vehicle")
    return redirect(next_url)


def km_alerts():
    """Unidades cuyo próximo mantenimiento (por kilometraje) está a
    KM_ALERT_THRESHOLD km o menos, según el kilometraje actual conocido.
    Usa el registro de mantenimiento más reciente de cada unidad que tenga
    next_due_km definido."""
    rows = query_all(
        """SELECT v.id, v.plate, v.current_km, m.next_due_km, m.type
           FROM vehicles v
           JOIN maintenance_records m ON m.vehicle_id = v.id
           WHERE m.next_due_km IS NOT NULL
           AND m.id = (
               SELECT m2.id FROM maintenance_records m2
               WHERE m2.vehicle_id = v.id AND m2.next_due_km IS NOT NULL
               ORDER BY m2.id DESC LIMIT 1
           )"""
    )
    alerts = []
    for r in rows:
        if r["current_km"] is None:
            continue
        remaining = r["next_due_km"] - r["current_km"]
        if remaining <= KM_ALERT_THRESHOLD:
            alerts.append(
                {
                    "plate": r["plate"],
                    "remaining_km": remaining,
                    "next_due_km": r["next_due_km"],
                    "current_km": r["current_km"],
                    "overdue": remaining <= 0,
                }
            )
    return alerts
