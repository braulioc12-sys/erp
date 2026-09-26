from datetime import datetime

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app.audit import get_creator_info, log_activity
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


def _order_label(record_id):
    """22 sep, registro de actividad (ver app/audit.py): etiqueta corta y
    consistente para cualquier log_activity() sobre una orden de
    mantenimiento (o algo que le pertenece, como un trabajo o material) --
    incluye la placa de la unidad para que se reconozca de un vistazo en la
    pantalla de Actividad, sin tener que abrir la orden. Se usa
    entity_type="orden_mantenimiento" en todas partes, incluso para
    trabajos/materiales/cuadrilla que en el schema son tablas aparte -- para
    Braulio esas acciones son parte del historial de LA ORDEN, no de un
    registro propio."""
    row = query_one(
        """SELECT m.id, v.plate FROM maintenance_records m
           JOIN vehicles v ON v.id = m.vehicle_id WHERE m.id = ?""",
        (record_id,),
    )
    if row is None:
        return f"Orden de mantenimiento #{record_id}"
    return f"Orden de mantenimiento #{record_id} — {row['plate']}"


def _lookup_mechanic(raw_mechanic_id):
    """18 sep, pedido de Braulio: cada fila de cuadrilla ahora también puede
    llevar el mecánico específico (del catálogo de Mecánicos), no solo el
    tipo+cantidad. Devuelve (mechanic_id, mechanic_name) o (None, None) si
    no se mandó nada o el id no existe (mismo criterio silencioso que el
    resto de esta función: una fila sin mecánico específico sigue siendo
    válida, solo queda con tipo+cantidad)."""
    raw_mechanic_id = (raw_mechanic_id or "").strip()
    if not raw_mechanic_id:
        return None, None
    mech = query_one("SELECT id, name FROM mechanics WHERE id = ?", (raw_mechanic_id,))
    if mech is None:
        return None, None
    return mech["id"], mech["name"]


def _crew_rows_from_form(job_id):
    """2 sep, pedido de Braulio: un trabajo ya no admite solo un tipo+
    cantidad de mecánico — puede tener varias combinaciones a la vez (ej.
    "1 Senior + 2 Junior" en un mismo cambio de aceite). El formulario manda
    varios campos con el mismo nombre `crew_type_<job_id>`/
    `crew_count_<job_id>`/`crew_mechanic_<job_id>` (uno por fila de
    cuadrilla agregada en el navegador) — se leen emparejados por posición,
    igual que ya se hace con `job_type_ids`/`material_ids` (checkboxes
    repetidos). Filas con cantidad inválida o tipo no reconocido se
    ignoran; si no llega ninguna fila válida, se usa una sola de "Otros" × 1
    (sin mecánico específico) como respaldo (nunca se deja un trabajo sin
    ninguna cuadrilla). `crew_mechanic_<job_id>` (18 sep, pedido de Braulio:
    "también se debe elegir el nombre de la base de registrados") es
    opcional por fila — puede venir vacío si esa fila se deja sin mecánico
    específico asignado."""
    types = request.form.getlist(f"crew_type_{job_id}")
    counts = request.form.getlist(f"crew_count_{job_id}")
    mechanic_ids = request.form.getlist(f"crew_mechanic_{job_id}")
    rows = []
    for i, (t, c) in enumerate(zip(types, counts)):
        t = t.strip()
        if t not in MECHANIC_TYPES:
            continue
        count = parse_float(c, None)
        if count is None or count < 1:
            continue
        mechanic_id, mechanic_name = _lookup_mechanic(mechanic_ids[i] if i < len(mechanic_ids) else "")
        rows.append((t, max(1, int(count)), mechanic_id, mechanic_name))
    return rows or [("Otros", 1, None, None)]


def _insert_selected_jobs(db, record_id, selected_jobs):
    """Inserta filas nuevas en maintenance_record_jobs para los trabajos
    marcados (sin dueño de cuadrilla propio — ver nota de la tabla en
    schema.sql), y una fila en maintenance_record_job_crew por cada
    combinación tipo+cantidad de mecánico que se haya armado para ese
    trabajo en el formulario (campos `crew_type_<id>`/`crew_count_<id>`).
    INSERT OR IGNORE en el trabajo por si ya estaba en la orden (evita un
    error de llave primaria duplicada, ej. dos envíos del mismo formulario)
    — en ese caso tampoco se duplica su cuadrilla. Devuelve la suma de
    minutos estimados efectivamente agregados."""
    total_minutes = 0
    for j in selected_jobs:
        cur = db.execute(
            """INSERT OR IGNORE INTO maintenance_record_jobs
               (maintenance_record_id, job_type_id, job_name, estimated_minutes)
               VALUES (?, ?, ?, ?)""",
            (record_id, j["id"], j["name"], j["estimated_minutes"]),
        )
        if cur.rowcount:
            total_minutes += j["estimated_minutes"]
            for mechanic_type, mechanic_count, mechanic_id, mechanic_name in _crew_rows_from_form(j["id"]):
                db.execute(
                    """INSERT INTO maintenance_record_job_crew
                       (maintenance_record_id, job_name, mechanic_type, mechanic_count, mechanic_id, mechanic_name)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (record_id, j["name"], mechanic_type, mechanic_count, mechanic_id, mechanic_name),
                )
    return total_minutes


def _job_crew(record_id, job_name, fallback_type=None, fallback_count=1, fallback_mechanic_id=None, fallback_mechanic_name=None):
    """Cuadrilla de un trabajo (lista de {mechanic_type, mechanic_count,
    mechanic_id, mechanic_name}), desde maintenance_record_job_crew. Si el
    trabajo no tiene ninguna fila ahí (orden creada antes del 2 sep, cuando
    el tipo/cantidad vivían directo en maintenance_record_jobs), se arma
    una cuadrilla de una sola fila a partir de esas columnas viejas — así
    una orden antigua se ve igual de bien sin necesitar ninguna migración
    de datos."""
    rows = query_all(
        "SELECT * FROM maintenance_record_job_crew WHERE maintenance_record_id = ? AND job_name = ? ORDER BY id",
        (record_id, job_name),
    )
    if rows:
        return rows
    if fallback_type:
        return [{
            "id": None, "mechanic_type": fallback_type, "mechanic_count": fallback_count,
            "mechanic_id": fallback_mechanic_id, "mechanic_name": fallback_mechanic_name,
        }]
    return []


def _crew_cost(crew_rows, minutes, labor_costs):
    total = 0.0
    for row in crew_rows:
        rate = float(labor_costs.get(row["mechanic_type"], "0") or 0)
        total += minutes * rate * (row["mechanic_count"] or 0)
    return total


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
    # 18 sep, pedido de Braulio: entrando desde "Historial y costos por
    # unidad" (mantenimiento.by_vehicle) el listado de órdenes debe quedar
    # solo de lectura -- ni abiertas ni terminadas se deben poder editar
    # desde ahí, es un reporte. Entrando por otro lado (ej. el botón
    # "Mantenimiento" del detalle de la unidad en Flota, que también manda
    # vehicle_id) sigue editable como siempre -- por eso esto depende de un
    # parámetro explícito (?readonly=1) y no de si vehicle_id está presente.
    readonly = request.args.get("readonly") == "1"
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
        query_one(
            """SELECT v.id, v.plate, v.current_km, v.current_km_updated_at, v.gps_km_error,
                      vl.odometer_km AS gps_odometer_km
               FROM vehicles v
               LEFT JOIN vehicle_locations vl ON vl.vehicle_id = v.id
               WHERE v.id = ?""",
            (vehicle_id,),
        )
        if vehicle_id else None
    )
    return render_template(
        "mantenimiento/list.html", records=records, jobs_by_record=jobs_by_record,
        status_by_record=status_by_record, order_status_labels=ORDER_STATUS_LABELS,
        vehicle_id=vehicle_id, filtered_vehicle=filtered_vehicle, readonly=readonly,
    )


@bp.route("/nuevo", methods=["GET", "POST"])
@permission_required("mantenimiento", "edit")
def new():
    vehicles = query_all("SELECT id, plate, current_km, current_km_updated_at FROM vehicles ORDER BY plate")
    # 2 sep, pedido de Braulio: al elegir la unidad en el formulario, el
    # cuadro de "Kilometraje Odómetro" se auto-llena con el último dato del
    # GPS (JS, ver form.html) — este mapa se lo entrega listo por id de
    # unidad, para no tener que ir a buscarlo por AJAX.
    vehicles_km = {
        str(v["id"]): {"km": v["current_km"], "updated_at": v["current_km_updated_at"]} for v in vehicles
    }
    job_types = get_catalog_jobs()
    materials = get_catalog_items()
    labor_costs = {t: get_setting(labor_cost_setting_key(t), "0") for t in MECHANIC_TYPES}
    # 18 sep, pedido de Braulio: elegir también el mecánico específico (del
    # catálogo) para cada fila de cuadrilla, no solo su tipo+cantidad.
    mechanics = get_catalog_mechanics()

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
                "mantenimiento/form.html", record=request.form, vehicles=vehicles, vehicles_km=vehicles_km,
                job_types=job_types, materials=materials, labor_costs=labor_costs,
                mechanic_types=MECHANIC_TYPES, mechanics=mechanics, today=today_str(),
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
        # 22 sep, registro de actividad (ver app/audit.py): se registra ni
        # bien queda insertada la orden, antes de agregarle los
        # trabajos/materiales marcados (que son parte de esta misma
        # creación, no ediciones aparte).
        log_activity(
            "mantenimiento", "CREAR", f"{_order_label(record_id)} — {record_type}",
            entity_type="orden_mantenimiento", entity_id=record_id,
            entity_url=url_for("mantenimiento.detail", record_id=record_id),
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
            # 15 sep, pedido de Braulio: "cuando ingrese una unidad debe
            # salir la opcion [de] que ya este disponible para programar" —
            # se puede marcar de una vez, al mismo momento de ingresarla a
            # mantenimiento (si no se marca acá, queda en 0/NO disponible
            # por defecto, y se puede marcar después desde Mantenimiento ->
            # Por unidad).
            available = 1 if request.form.get("available_for_scheduling") else 0
            execute(
                "UPDATE vehicles SET status = 'MANTENIMIENTO', available_for_scheduling = ? WHERE id = ?",
                (available, vehicle_id),
            )

        flash("Mantenimiento registrado.", "success")
        return redirect(url_for("mantenimiento.list_view"))

    return render_template(
        "mantenimiento/form.html", record=None, vehicles=vehicles, vehicles_km=vehicles_km,
        job_types=job_types, materials=materials, labor_costs=labor_costs,
        mechanic_types=MECHANIC_TYPES, mechanics=mechanics, today=today_str(),
    )


@bp.route("/<int:record_id>/eliminar", methods=["POST"])
@permission_required("mantenimiento", "edit")
def delete(record_id):
    if not validate_csrf():
        abort(400)
    # 22 sep, registro de actividad: la etiqueta se arma ANTES de borrar
    # (después ya no habría de dónde sacar la placa de la unidad -- ver
    # _order_label()), pero se registra recién después de que el borrado ya
    # se hizo con éxito, como el resto de log_activity().
    label = _order_label(record_id)
    execute("DELETE FROM maintenance_record_jobs WHERE maintenance_record_id = ?", (record_id,))
    execute("DELETE FROM maintenance_record_materials WHERE maintenance_record_id = ?", (record_id,))
    execute("DELETE FROM maintenance_records WHERE id = ?", (record_id,))
    log_activity(
        "mantenimiento", "ELIMINAR", label,
        entity_type="orden_mantenimiento", entity_id=record_id,
    )
    flash("Registro de mantenimiento eliminado.", "success")
    return redirect(url_for("mantenimiento.list_view"))


# --- Detalle de una orden: marcar trabajos terminados/pendientes y asignar mecánico ---

@bp.route("/<int:record_id>")
@permission_required("mantenimiento", "view")
def detail(record_id):
    # 15 sep, pedido de Braulio (ajuste): "una vez creada la orden, ahi es
    # donde el administrador y personal de mantenimiento... pueden
    # habilitarla como disponible para programar" -- se agrega
    # vehicle_status/vehicle_available_for_scheduling acá para poder
    # mostrar y togglear la opción directamente desde el detalle de la
    # orden (además de seguir estando en Mantenimiento -> Por unidad y en
    # la casilla al registrar el ingreso a mantenimiento).
    record = query_one(
        """SELECT m.*, v.plate as vehicle_plate, v.status as vehicle_status,
                  v.available_for_scheduling as vehicle_available_for_scheduling
           FROM maintenance_records m
           JOIN vehicles v ON v.id = m.vehicle_id WHERE m.id = ?""",
        (record_id,),
    )
    if record is None:
        abort(404)
    # 18 sep, pedido de Braulio: ver nota en list_view() -- mismo flag,
    # propagado desde ahí cuando se entra a la orden por "Ver historial".
    readonly = request.args.get("readonly") == "1"
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
    crew_by_job = {}
    labor_cost_by_job = {}
    for j in jobs:
        crew = _job_crew(record_id, j["job_name"], j["mechanic_type"], j["mechanic_count"], j["mechanic_id"], j["mechanic_name"])
        crew_by_job[j["job_name"]] = crew
        labor_cost_by_job[j["job_name"]] = _crew_cost(crew, j["estimated_minutes"], labor_costs)
    # 22 sep, pedido de Braulio ("que usuario creo... la orden"): quién y
    # cuándo se creó, según activity_log (ver app/audit.py) -- None para
    # órdenes de antes de que existiera este registro.
    creator = get_creator_info("orden_mantenimiento", record_id)
    return render_template(
        "mantenimiento/detail.html", record=record, jobs=jobs, materials=materials, mechanics=mechanics,
        order_status=_order_status(jobs), order_status_labels=ORDER_STATUS_LABELS,
        mechanic_types=MECHANIC_TYPES, available_job_types=available_job_types,
        available_materials=available_materials, labor_costs=labor_costs, materials_total=materials_total,
        crew_by_job=crew_by_job, labor_cost_by_job=labor_cost_by_job, readonly=readonly, creator=creator,
    )


@bp.route("/<int:record_id>/trabajos/cuadrilla/agregar", methods=["POST"])
@permission_required("mantenimiento", "edit")
def job_crew_add(record_id):
    """2 sep, pedido de Braulio: agregar una combinación tipo+cantidad de
    mecánico más a un trabajo YA en la orden (ej. ya tenía "1 Senior" y se
    le suma "2 Junior") — sin reemplazar lo que ya tenía."""
    if not validate_csrf():
        abort(400)
    job_name = request.form.get("job_name", "")
    job = query_one(
        "SELECT * FROM maintenance_record_jobs WHERE maintenance_record_id = ? AND job_name = ?",
        (record_id, job_name),
    )
    if job is None:
        abort(404)
    mechanic_type = request.form.get("mechanic_type", "").strip()
    if mechanic_type not in MECHANIC_TYPES:
        flash("Elige un tipo de mecánico válido.", "error")
        return redirect(url_for("mantenimiento.detail", record_id=record_id))
    count = parse_float(request.form.get("mechanic_count"), 1) or 1
    count = max(1, int(count))
    # 18 sep, pedido de Braulio: además del tipo, esta fila de cuadrilla
    # también puede llevar el mecánico específico (del catálogo).
    mechanic_id, mechanic_name = _lookup_mechanic(request.form.get("mechanic_id"))
    # Si el trabajo todavía no tenía ninguna fila propia en la cuadrilla
    # nueva (orden vieja, con el tipo/cantidad guardado directo en
    # maintenance_record_jobs), primero se traslada esa fila implícita acá
    # para no perderla al agregar la nueva.
    if not query_one(
        "SELECT id FROM maintenance_record_job_crew WHERE maintenance_record_id = ? AND job_name = ?",
        (record_id, job_name),
    ) and job["mechanic_type"]:
        execute(
            """INSERT INTO maintenance_record_job_crew
               (maintenance_record_id, job_name, mechanic_type, mechanic_count, mechanic_id, mechanic_name)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (record_id, job_name, job["mechanic_type"], job["mechanic_count"] or 1, job["mechanic_id"], job["mechanic_name"]),
        )
    execute(
        """INSERT INTO maintenance_record_job_crew
           (maintenance_record_id, job_name, mechanic_type, mechanic_count, mechanic_id, mechanic_name)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (record_id, job_name, mechanic_type, count, mechanic_id, mechanic_name),
    )
    crew_label = f'{count} × {mechanic_type}' + (f' ({mechanic_name})' if mechanic_name else '')
    log_activity(
        "mantenimiento", "EDITAR", f'{_order_label(record_id)}: agregó {crew_label} a "{job_name}"',
        entity_type="orden_mantenimiento", entity_id=record_id,
        entity_url=url_for("mantenimiento.detail", record_id=record_id),
    )
    flash(f'Se agregó {crew_label} a "{job_name}".', "success")
    return redirect(url_for("mantenimiento.detail", record_id=record_id))


@bp.route("/<int:record_id>/trabajos/cuadrilla/<int:crew_id>/eliminar", methods=["POST"])
@permission_required("mantenimiento", "edit")
def job_crew_remove(record_id, crew_id):
    if not validate_csrf():
        abort(400)
    crew = query_one(
        "SELECT * FROM maintenance_record_job_crew WHERE id = ? AND maintenance_record_id = ?",
        (crew_id, record_id),
    )
    if crew is None:
        abort(404)
    execute("DELETE FROM maintenance_record_job_crew WHERE id = ?", (crew_id,))
    log_activity(
        "mantenimiento", "EDITAR",
        f'{_order_label(record_id)}: quitó {crew["mechanic_count"]} × {crew["mechanic_type"]} de "{crew["job_name"]}"',
        entity_type="orden_mantenimiento", entity_id=record_id,
        entity_url=url_for("mantenimiento.detail", record_id=record_id),
    )
    flash("Se quitó esa combinación de mecánico del trabajo.", "success")
    return redirect(url_for("mantenimiento.detail", record_id=record_id))


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
    added_names = [j["name"] for j in selected_jobs] + [m["name"] for m in selected_materials]
    log_activity(
        "mantenimiento", "EDITAR", f"{_order_label(record_id)}: agregó {', '.join(added_names)}",
        entity_type="orden_mantenimiento", entity_id=record_id,
        entity_url=url_for("mantenimiento.detail", record_id=record_id),
    )
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
    log_activity(
        "mantenimiento", "EDITAR", f'{_order_label(record_id)}: cantidad de mecánicos de "{job_name}" = {count}',
        entity_type="orden_mantenimiento", entity_id=record_id,
        entity_url=url_for("mantenimiento.detail", record_id=record_id),
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
    log_activity(
        "mantenimiento", "ESTADO", f'{_order_label(record_id)}: "{job_name}" → {new_status}',
        entity_type="orden_mantenimiento", entity_id=record_id,
        entity_url=url_for("mantenimiento.detail", record_id=record_id),
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
        log_activity(
            "mantenimiento", "EDITAR", f'{_order_label(record_id)}: quitó el mecánico asignado a "{job_name}"',
            entity_type="orden_mantenimiento", entity_id=record_id,
            entity_url=url_for("mantenimiento.detail", record_id=record_id),
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
        log_activity(
            "mantenimiento", "EDITAR", f'{_order_label(record_id)}: "{mechanic["name"]}" asignado a "{job_name}"',
            entity_type="orden_mantenimiento", entity_id=record_id,
            entity_url=url_for("mantenimiento.detail", record_id=record_id),
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
    log_activity(
        "mantenimiento", "EDITAR", f'{_order_label(record_id)}: tipo de mecánico de "{job_name}" = {mechanic_type}',
        entity_type="orden_mantenimiento", entity_id=record_id,
        entity_url=url_for("mantenimiento.detail", record_id=record_id),
    )
    flash(f'Tipo de mecánico de "{job_name}" actualizado a {mechanic_type}.', "success")
    return redirect(url_for("mantenimiento.detail", record_id=record_id))


# --- Trabajos de mantenimiento (catálogo con tiempo estimado) ---

def get_catalog_jobs(only_active=True):
    # 15 sep, pedido de Braulio: "ordena el menu de trabajos por orden
    # alfabetico" -- antes se ordenaba por sort_order (el orden en que se
    # cargaron/agregaron), que no tiene ninguna relación con el nombre. Ya
    # no hay ninguna pantalla que reordene sort_order a mano, así que
    # cambiar el ORDER BY a "name" no pierde nada -- la columna sigue
    # existiendo en la tabla, solo dejó de usarse para mostrar la lista.
    sql = "SELECT * FROM maintenance_job_types WHERE 1=1"
    if only_active:
        sql += " AND active = 1"
    sql += " ORDER BY name"
    return query_all(sql)


@bp.route("/trabajos")
@permission_required("mantenimiento", "view")
def jobs_list():
    jobs = query_all("SELECT * FROM maintenance_job_types ORDER BY name")
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
            log_activity(
                "mantenimiento", "REACTIVAR", f'Trabajo de catálogo "{name}"',
                entity_type="tipo_trabajo_mantenimiento", entity_id=existing["id"],
                entity_url=url_for("mantenimiento.jobs_list"),
            )
            flash(f'"{name}" reactivado.', "success")
    else:
        max_order = query_one("SELECT COALESCE(MAX(sort_order), -1) m FROM maintenance_job_types")["m"]
        job_id = execute(
            "INSERT INTO maintenance_job_types (name, estimated_minutes, sort_order) VALUES (?, ?, ?)",
            (name, int(minutes), max_order + 1),
        )
        log_activity(
            "mantenimiento", "CREAR", f'Trabajo de catálogo "{name}"',
            entity_type="tipo_trabajo_mantenimiento", entity_id=job_id,
            entity_url=url_for("mantenimiento.jobs_list"),
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
    # 22 sep, registro de actividad: reemplazo masivo, no hay un solo
    # entity_id que tenga sentido (se borró el catálogo entero) -- se
    # registra sin entity_type/entity_id, igual queda el "quién y cuándo".
    log_activity(
        "mantenimiento", "REEMPLAZAR", f"Catálogo de trabajos reemplazado ({len(DEFAULT_JOB_TYPES)} trabajos)",
        entity_url=url_for("mantenimiento.jobs_list"),
    )
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
    log_activity(
        "mantenimiento", "DESACTIVAR" if job["active"] else "REACTIVAR", f'Trabajo de catálogo "{job["name"]}"',
        entity_type="tipo_trabajo_mantenimiento", entity_id=job_id,
        entity_url=url_for("mantenimiento.jobs_list"),
    )
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
            log_activity(
                "mantenimiento", "REACTIVAR", f'Mecánico "{name}"',
                entity_type="mecanico", entity_id=existing["id"],
                entity_url=url_for("mantenimiento.mechanics_list"),
            )
            flash(f'"{name}" reactivado.', "success")
    else:
        max_order = query_one("SELECT COALESCE(MAX(sort_order), -1) m FROM mechanics")["m"]
        mechanic_id = execute(
            "INSERT INTO mechanics (name, mechanic_type, sort_order) VALUES (?, ?, ?)",
            (name, mechanic_type, max_order + 1),
        )
        log_activity(
            "mantenimiento", "CREAR", f'Mecánico "{name}" ({mechanic_type})',
            entity_type="mecanico", entity_id=mechanic_id,
            entity_url=url_for("mantenimiento.mechanics_list"),
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
    log_activity(
        "mantenimiento", "EDITAR", f'Mecánico "{mechanic["name"]}": tipo = {mechanic_type}',
        entity_type="mecanico", entity_id=mechanic_id,
        entity_url=url_for("mantenimiento.mechanics_list"),
    )
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
    log_activity(
        "mantenimiento", "DESACTIVAR" if mechanic["active"] else "REACTIVAR", f'Mecánico "{mechanic["name"]}"',
        entity_type="mecanico", entity_id=mechanic_id,
        entity_url=url_for("mantenimiento.mechanics_list"),
    )
    flash("Actualizado." if mechanic["active"] else "Reactivado.", "success")
    return redirect(url_for("mantenimiento.mechanics_list"))


# --- Historial y costos por unidad ---

@bp.route("/por-unidad")
@permission_required("mantenimiento", "view")
def by_vehicle():
    summary = query_all(
        """SELECT v.id, v.plate, v.current_km, v.current_km_updated_at, v.status, v.available_for_scheduling,
                  v.gps_km_error, MAX(vl.odometer_km) AS gps_odometer_km,
                  COUNT(m.id) as n_records,
                  COALESCE(SUM(m.cost), 0) as total_cost,
                  MAX(m.maintenance_date) as last_date
           FROM vehicles v
           LEFT JOIN maintenance_records m ON m.vehicle_id = v.id
           LEFT JOIN vehicle_locations vl ON vl.vehicle_id = v.id
           GROUP BY v.id
           ORDER BY v.plate"""
    )
    return render_template("mantenimiento/by_vehicle.html", summary=summary)


@bp.route("/unidad/<int:vehicle_id>/ingresar-mantenimiento", methods=["POST"])
@permission_required("mantenimiento", "edit")
def set_vehicle_maintenance_status(vehicle_id):
    """16 sep, pedido de Braulio: "Hay que activar la opcion de ingresar a
    mantenimiento tambien luego de crear la orden, sin necesidad de editar
    unidad desde flota." Antes, si no se marcaba la casilla
    "mark_in_maintenance" al registrar la orden (ver new()), la única forma
    de pasar la unidad a estado MANTENIMIENTO era por Flota -> Editar
    unidad -- ahora se puede hacer también desde acá (detalle de la orden,
    o Por unidad), sin salir del módulo de Mantenimiento. No toca
    available_for_scheduling (queda en 0, como ya lo deja
    flota.edit_vehicle() para cualquier unidad que no esté en
    mantenimiento) -- se marca disponible aparte, con
    set_vehicle_available_for_scheduling, una vez que esto ya está hecho."""
    if not validate_csrf():
        abort(400)
    vehicle = query_one("SELECT id, plate, status FROM vehicles WHERE id = ?", (vehicle_id,))
    if vehicle is None:
        abort(404)
    next_url = request.form.get("next") or url_for("mantenimiento.by_vehicle")
    if vehicle["status"] == "MANTENIMIENTO":
        flash("Esa unidad ya está en mantenimiento.", "info")
        return redirect(next_url)
    execute("UPDATE vehicles SET status = 'MANTENIMIENTO' WHERE id = ?", (vehicle_id,))
    log_activity(
        "mantenimiento", "ESTADO", f'Unidad "{vehicle["plate"]}": {vehicle["status"]} → MANTENIMIENTO',
        entity_type="vehiculo", entity_id=vehicle_id,
    )
    flash(f'"{vehicle["plate"]}" marcada como en mantenimiento.', "success")
    return redirect(next_url)


@bp.route("/unidad/<int:vehicle_id>/disponible-programar", methods=["POST"])
@permission_required("mantenimiento", "edit")
def set_vehicle_available_for_scheduling(vehicle_id):
    """15 sep, pedido de Braulio: "cuando ingrese una unidad debe salir la
    opcion [de] que ya este disponible para programar... esta opcion solo
    la puede habilitar el administrador y el personal de mantenimiento."
    Alcanza con exigir permiso "mantenimiento":"edit" -- en este sistema
    eso es exactamente Administrador + Mecánico (ver PERMISSIONS en
    app/auth.py); Despachador/Operador/Almacén solo tienen "ver" y
    Contabilidad no tiene acceso a Mantenimiento -- no hace falta ningún
    chequeo de rol adicional acá. Solo tiene efecto mientras la unidad
    está en mantenimiento; fuera de eso no hay nada que "programar" (la
    unidad ya está disponible por default)."""
    if not validate_csrf():
        abort(400)
    vehicle = query_one("SELECT id, plate, status FROM vehicles WHERE id = ?", (vehicle_id,))
    if vehicle is None:
        abort(404)
    next_url = request.form.get("next") or url_for("mantenimiento.by_vehicle")
    if vehicle["status"] != "MANTENIMIENTO":
        flash("Esta opción solo aplica mientras la unidad está en mantenimiento.", "error")
        return redirect(next_url)
    available = 1 if request.form.get("available") == "1" else 0
    execute("UPDATE vehicles SET available_for_scheduling = ? WHERE id = ?", (available, vehicle_id))
    log_activity(
        "mantenimiento", "EDITAR",
        f'Unidad "{vehicle["plate"]}": disponible para programar = {"sí" if available else "no"}',
        entity_type="vehiculo", entity_id=vehicle_id,
    )
    flash(
        f'"{vehicle["plate"]}" marcada como {"disponible" if available else "NO disponible"} para programar viajes mientras está en mantenimiento.',
        "success",
    )
    return redirect(next_url)


@bp.route("/unidad/<int:vehicle_id>/gps-km-error", methods=["POST"])
@permission_required("mantenimiento", "edit")
def set_vehicle_gps_km_error(vehicle_id):
    """26 sep, pedido de Braulio ("hay algunas unidades que el kilometraje
    del gps es distinto al fisico... podemos habilitar la opcion que diga
    en la unidad GPS error kilometraje"): atajo para activar/desactivar el
    flag `gps_km_error` directamente desde "Por unidad", sin ir a Flota ->
    Editar unidad (mismo criterio que "Marcar disponible"/"Corregir
    kilometraje" ya en esta misma pantalla). Mientras está activo,
    perform_frotcom_sync() (integraciones.py) deja de actualizar
    `current_km` con el dato del GPS -- ver ese archivo para el detalle."""
    if not validate_csrf():
        abort(400)
    vehicle = query_one("SELECT id, plate FROM vehicles WHERE id = ?", (vehicle_id,))
    if vehicle is None:
        abort(404)
    gps_km_error = 1 if request.form.get("gps_km_error") == "1" else 0
    execute("UPDATE vehicles SET gps_km_error = ? WHERE id = ?", (gps_km_error, vehicle_id))
    log_activity(
        "mantenimiento", "EDITAR",
        f'Unidad "{vehicle["plate"]}": GPS con error de kilometraje = {"sí" if gps_km_error else "no"}',
        entity_type="vehiculo", entity_id=vehicle_id,
    )
    flash(
        f'"{vehicle["plate"]}": {"marcada con GPS con error de kilometraje (el GPS ya no actualiza su kilometraje automáticamente)" if gps_km_error else "el GPS vuelve a actualizar su kilometraje automáticamente"}.',
        "success",
    )
    next_url = request.form.get("next") or url_for("mantenimiento.by_vehicle")
    return redirect(next_url)


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
    vehicle = query_one("SELECT id, plate FROM vehicles WHERE id = ?", (vehicle_id,))
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
        log_activity(
            "mantenimiento", "EDITAR", f'Unidad "{vehicle["plate"]}": kilometraje corregido a {new_km:g} km',
            entity_type="vehiculo", entity_id=vehicle_id,
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


def maintenance_date_alerts():
    """Mantenimientos cuya próxima fecha (next_due_date) está a 30 días o
    menos, o ya vencida -- la usan el Panel y las alertas por correo (ver
    app/alerts.py). Estaba escrita directo en dashboard.index(); se separó
    acá (20 sep) para no repetir la misma consulta en los dos lugares."""
    rows = query_all(
        """SELECT v.plate, m.next_due_date FROM maintenance_records m
           JOIN vehicles v ON v.id = m.vehicle_id
           WHERE m.next_due_date IS NOT NULL AND m.next_due_date != ''
           AND date(m.next_due_date) <= date('now', '+30 days')
           ORDER BY m.next_due_date ASC"""
    )
    today = today_str()
    return [
        {"plate": r["plate"], "next_due_date": r["next_due_date"], "overdue": r["next_due_date"] < today}
        for r in rows
    ]
