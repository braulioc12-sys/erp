from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app.auth import permission_required, validate_csrf
from app.db import execute, get_db, query_all, query_one
from app.helpers import parse_date, parse_float, today_str
from app.routes.catalogos import get_catalog

bp = Blueprint("mantenimiento", __name__, url_prefix="/mantenimiento")

# Umbral de kilómetros para mostrar la alerta de "mantenimiento próximo" en el panel.
KM_ALERT_THRESHOLD = 1000


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
    if records:
        ids = [r["id"] for r in records]
        placeholders = ",".join("?" * len(ids))
        rows = query_all(
            f"SELECT * FROM maintenance_record_jobs WHERE maintenance_record_id IN ({placeholders})", ids
        )
        for row in rows:
            jobs_by_record.setdefault(row["maintenance_record_id"], []).append(row["job_name"])

    filtered_vehicle = query_one("SELECT plate FROM vehicles WHERE id = ?", (vehicle_id,)) if vehicle_id else None
    return render_template(
        "mantenimiento/list.html", records=records, jobs_by_record=jobs_by_record,
        vehicle_id=vehicle_id, filtered_vehicle=filtered_vehicle,
    )


@bp.route("/nuevo", methods=["GET", "POST"])
@permission_required("mantenimiento", "edit")
def new():
    vehicles = query_all("SELECT id, plate, current_km FROM vehicles ORDER BY plate")
    concepts = get_catalog("maintenance_type")
    job_types = get_catalog_jobs()

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        vehicle_id = request.form.get("vehicle_id")
        maintenance_date = parse_date(request.form.get("maintenance_date")) or today_str()
        record_type = request.form.get("type", "").strip()
        odometer_km = parse_float(request.form.get("odometer_km"), None)
        job_ids = [int(j) for j in request.form.getlist("job_type_ids")]

        errors = []
        if not vehicle_id:
            errors.append("Selecciona una unidad.")
        if not record_type:
            errors.append("Indica el concepto de mantenimiento.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "mantenimiento/form.html", record=request.form, vehicles=vehicles,
                concepts=concepts, job_types=job_types,
            )

        selected_jobs = [j for j in job_types if j["id"] in job_ids]
        estimated_minutes = sum(j["estimated_minutes"] for j in selected_jobs) or None

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

        if selected_jobs:
            db = get_db()
            for j in selected_jobs:
                db.execute(
                    """INSERT INTO maintenance_record_jobs (maintenance_record_id, job_type_id, job_name, estimated_minutes)
                       VALUES (?, ?, ?, ?)""",
                    (record_id, j["id"], j["name"], j["estimated_minutes"]),
                )
            db.commit()

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
        "mantenimiento/form.html", record=None, vehicles=vehicles, concepts=concepts,
        job_types=job_types, today=today_str(),
    )


@bp.route("/<int:record_id>/eliminar", methods=["POST"])
@permission_required("mantenimiento", "edit")
def delete(record_id):
    if not validate_csrf():
        abort(400)
    execute("DELETE FROM maintenance_record_jobs WHERE maintenance_record_id = ?", (record_id,))
    execute("DELETE FROM maintenance_records WHERE id = ?", (record_id,))
    flash("Registro de mantenimiento eliminado.", "success")
    return redirect(url_for("mantenimiento.list_view"))


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
    return render_template("mantenimiento/jobs.html", jobs=jobs)


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


# --- Historial y costos por unidad ---

@bp.route("/por-unidad")
@permission_required("mantenimiento", "view")
def by_vehicle():
    summary = query_all(
        """SELECT v.id, v.plate, COUNT(m.id) as n_records,
                  COALESCE(SUM(m.cost), 0) as total_cost,
                  MAX(m.maintenance_date) as last_date
           FROM vehicles v
           LEFT JOIN maintenance_records m ON m.vehicle_id = v.id
           GROUP BY v.id
           ORDER BY v.plate"""
    )
    return render_template("mantenimiento/by_vehicle.html", summary=summary)


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
