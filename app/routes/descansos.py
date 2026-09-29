"""Descansos laborales de conductores (22 sep, pedido de Braulio: "los
conductores pueden trabajar hasta 6 dias seguidos y luego descansar 1, o
trabajar 12 dias seguidos y luego descansan 2. En este modulo quiero poder
registrar los dias que han descansado").

Aclarado con Braulio (AskUserQuestion, misma fecha): las dos reglas NO son
dos "turnos" distintos que se le asignan a cada conductor -- se usan LAS DOS
A LA VEZ como una sola regla de cumplimiento: a los 6 días seguidos ya le
corresponde descansar (mínimo 1 día), pero puede seguir trabajando hasta un
tope absoluto de 12 días si al final descansa al menos 2 días. Por eso acá
no existe un "ciclo" guardado por conductor -- se calculan los días
trabajados seguidos desde su último descanso registrado y se compara contra
ambos límites (WORK_LIMIT_SOON / WORK_LIMIT_MAX) para decidir el estado.

Supuesto importante (documentarlo también en README): "días trabajados
seguidos" se cuenta en días CALENDARIO desde el día siguiente al fin del
último descanso registrado AQUÍ -- no se cruza contra los viajes reales del
conductor día por día. Si un conductor todavía no tiene ningún descanso
registrado, se usa como punto de partida el más antiguo entre su primer
viaje (trips.scheduled_date) y su fecha de alta (drivers.created_at), para
no inventar una alerta descabellada en el primer arranque de este módulo.
Este módulo es una BITÁCORA de cumplimiento, no un sistema de asistencia:
si un conductor descansó un día sin que nadie lo registre acá, el sistema
no tiene forma de saberlo y seguirá contando ese día como trabajado."""
import datetime as dt

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for

from app.audit import log_activity
from app.auth import permission_required, validate_csrf
from app.bulk_import import DRIVER_REST_COLUMNS, DRIVER_REST_EXAMPLE, XLSX_MIME, build_import_template, read_import_rows
from app.db import execute, query_all, query_one
from app.helpers import today_str

bp = Blueprint("descansos", __name__, url_prefix="/descansos")

WORK_LIMIT_SOON = 6   # días trabajados seguidos a partir de los cuales ya le toca descansar ("Atención")
WORK_LIMIT_MAX = 12   # tope absoluto de días trabajados seguidos ("Urgente")
REST_MIN_SHORT = 1    # descanso mínimo si trabajó WORK_LIMIT_SOON días o menos
REST_MIN_LONG = 2     # descanso mínimo si trabajó más de WORK_LIMIT_SOON días (hasta el tope)

STATUS_ORDER = {"URGENTE": 0, "ATENCION": 1, "DESCANSANDO": 2, "OK": 3, "SIN_DATOS": 4}


def _to_date(value):
    """Convierte 'YYYY-MM-DD' o 'YYYY-MM-DD HH:MM:SS' a date. None si no se
    puede leer (valor vacío, None, o formato inesperado)."""
    if not value:
        return None
    try:
        return dt.datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _last_rest(driver_id):
    return query_one(
        "SELECT * FROM driver_rests WHERE driver_id = ? ORDER BY end_date DESC, id DESC LIMIT 1",
        (driver_id,),
    )


def _earliest_trip_date(driver_id):
    """Primer viaje conocido del conductor (como titular o segundo
    conductor) -- ver el comentario del módulo sobre el punto de partida
    cuando todavía no tiene ningún descanso registrado."""
    row = query_one(
        "SELECT MIN(scheduled_date) d FROM trips WHERE driver_id = ? OR driver2_id = ?",
        (driver_id, driver_id),
    )
    return _to_date(row["d"]) if row else None


def driver_rest_status(driver, as_of=None):
    """Calcula el estado de descanso de UN conductor (Row/dict de la tabla
    drivers). Devuelve un dict:
      - last_rest: Row del último descanso registrado, o None.
      - status: 'DESCANSANDO' | 'OK' | 'ATENCION' | 'URGENTE' | 'SIN_DATOS'.
      - streak_days: días trabajados seguidos a la fecha `as_of` (por
        defecto hoy), o None si está descansando o no hay datos.
      - resting_until: fecha en que termina el descanso actual, si
        status == 'DESCANSANDO'.
    `as_of` permite calcular "¿cuántos días llevaba trabajando ANTES de tal
    fecha?" -- lo usa new() para avisar si un descanso recién registrado es
    más corto de lo que tocaría."""
    as_of = as_of or dt.date.today()
    last_rest = _last_rest(driver["id"])

    if last_rest:
        start = _to_date(last_rest["start_date"])
        end = _to_date(last_rest["end_date"])
        if start and end and start <= as_of <= end:
            return {"last_rest": last_rest, "status": "DESCANSANDO", "streak_days": None, "resting_until": last_rest["end_date"]}
        baseline = (end + dt.timedelta(days=1)) if end else None
    else:
        baseline = _earliest_trip_date(driver["id"]) or _to_date(driver["created_at"])

    if baseline is None:
        return {"last_rest": last_rest, "status": "SIN_DATOS", "streak_days": None, "resting_until": None}

    streak_days = max((as_of - baseline).days + 1, 0)
    if streak_days >= WORK_LIMIT_MAX:
        status = "URGENTE"
    elif streak_days >= WORK_LIMIT_SOON:
        status = "ATENCION"
    else:
        status = "OK"
    return {"last_rest": last_rest, "status": status, "streak_days": streak_days, "resting_until": None}


def all_driver_statuses(q=None):
    """Estado de descanso de todos los conductores activos, ordenado
    mostrando primero los más urgentes -- para la lista del módulo.

    22 sep, pedido de Braulio ("este debe tener un boton de busqueda de
    conductor arriba"): `q` filtra por nombre -- LOWER() en ambos lados
    (mismo patrón que clientes.list_view) para que funcione igual en
    SQLite (local) y Postgres (producción)."""
    if q:
        drivers = query_all(
            "SELECT * FROM drivers WHERE status = 'ACTIVO' AND LOWER(name) LIKE LOWER(?) ORDER BY name",
            (f"%{q}%",),
        )
    else:
        drivers = query_all("SELECT * FROM drivers WHERE status = 'ACTIVO' ORDER BY name")
    result = [{"driver": d, **driver_rest_status(d)} for d in drivers]
    result.sort(key=lambda r: (STATUS_ORDER.get(r["status"], 9), -(r["streak_days"] or 0)))
    return result


def descansos_alerts():
    """Para el Panel (dashboard/index.html) -- mismo patrón que
    document_alerts() en conductores.py: conductores que ya deberían
    descansar (ATENCION) o que superaron el tope absoluto (URGENTE)."""
    return [
        {
            "name": r["driver"]["name"],
            "streak_days": r["streak_days"],
            "status": r["status"],
            "overdue": r["status"] == "URGENTE",
        }
        for r in all_driver_statuses()
        if r["status"] in ("ATENCION", "URGENTE")
    ]


def _form_context(mode, driver=None, drivers=None, selected_driver_id=None, hint=None, rest=None, rest_id=None):
    return {
        "mode": mode,
        "driver": driver,
        "drivers": drivers if drivers is not None else [],
        "selected_driver_id": selected_driver_id,
        "hint": hint,
        "rest": rest,
        "rest_id": rest_id,
        "today": today_str(),
    }


@bp.route("")
@permission_required("descansos", "view")
def list_view():
    q = request.args.get("q", "").strip()
    driver_id = request.args.get("driver_id", type=int)
    if driver_id:
        rests = query_all(
            """SELECT r.*, d.name AS driver_name FROM driver_rests r
               JOIN drivers d ON d.id = r.driver_id
               WHERE r.driver_id = ? ORDER BY r.start_date DESC, r.id DESC""",
            (driver_id,),
        )
    else:
        rests = query_all(
            """SELECT r.*, d.name AS driver_name FROM driver_rests r
               JOIN drivers d ON d.id = r.driver_id
               ORDER BY r.start_date DESC, r.id DESC"""
        )
    drivers = query_all("SELECT * FROM drivers WHERE status = 'ACTIVO' ORDER BY name")
    return render_template(
        "descansos/list.html",
        statuses=all_driver_statuses(q),
        rests=rests,
        drivers=drivers,
        selected_driver=driver_id,
        limits={"soon": WORK_LIMIT_SOON, "max": WORK_LIMIT_MAX},
        q=q,
    )


@bp.route("/nuevo", methods=["GET", "POST"])
@permission_required("descansos", "edit")
def new():
    drivers = query_all("SELECT * FROM drivers WHERE status = 'ACTIVO' ORDER BY name")

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        driver_id = request.form.get("driver_id", type=int)
        driver = query_one("SELECT * FROM drivers WHERE id = ?", (driver_id,)) if driver_id else None
        start = _to_date(request.form.get("start_date"))
        end = _to_date(request.form.get("end_date"))

        if not driver:
            flash("Selecciona un conductor.", "error")
            return render_template("descansos/form.html", **_form_context("new", drivers=drivers, rest=request.form))
        if not start or not end:
            flash("Ingresa una fecha de inicio y de fin válidas.", "error")
            return render_template("descansos/form.html", **_form_context(
                "new", driver=driver, drivers=drivers, selected_driver_id=driver_id, rest=request.form
            ))
        if end < start:
            flash("La fecha de fin no puede ser anterior a la fecha de inicio.", "error")
            return render_template("descansos/form.html", **_form_context(
                "new", driver=driver, drivers=drivers, selected_driver_id=driver_id, rest=request.form
            ))

        days_count = (end - start).days + 1
        notes = request.form.get("notes", "").strip()

        # Días trabajados seguidos justo ANTES de este descanso, para avisar
        # (sin bloquear -- mismo criterio conservador que el resto del
        # sistema, ver la advertencia de detracción en facturación.new()) si
        # el descanso registrado es más corto que el mínimo que le tocaría.
        streak_before = driver_rest_status(driver, as_of=start - dt.timedelta(days=1))["streak_days"] or 0
        required_min = REST_MIN_LONG if streak_before > WORK_LIMIT_SOON else REST_MIN_SHORT

        rest_id = execute(
            "INSERT INTO driver_rests (driver_id, start_date, end_date, days_count, notes) VALUES (?, ?, ?, ?, ?)",
            (driver_id, request.form.get("start_date"), request.form.get("end_date"), days_count, notes),
        )
        # 22 sep, registro de actividad (ver app/audit.py): este módulo es una
        # bitácora sin pantalla de detalle propia -- se registra igual para
        # que aparezca en Actividad, sin "Creado por" en pantalla (pedido de
        # Braulio, ver notas del encargo).
        log_activity(
            "descansos", "CREAR",
            f"Descanso de {driver['name']}: {request.form.get('start_date')} a {request.form.get('end_date')} ({days_count} día(s))",
            entity_type="descanso", entity_id=rest_id,
        )
        flash(f"Descanso registrado: {days_count} día(s) para {driver['name']}.", "success")
        if days_count < required_min:
            flash(
                f"{driver['name']} llevaba {streak_before} día(s) trabajados seguidos antes de este descanso -- "
                f"según la norma le correspondían al menos {required_min} día(s) de descanso. Se registró igual; "
                f"verifica las fechas si fue un error.",
                "error",
            )
        return redirect(url_for("descansos.list_view", driver_id=driver_id))

    driver_id = request.args.get("driver_id", type=int)
    driver = query_one("SELECT * FROM drivers WHERE id = ?", (driver_id,)) if driver_id else None
    hint = driver_rest_status(driver) if driver else None
    return render_template("descansos/form.html", **_form_context(
        "new", driver=driver, drivers=drivers, selected_driver_id=driver_id, hint=hint
    ))


@bp.route("/<int:rest_id>/editar", methods=["GET", "POST"])
@permission_required("descansos", "edit")
def edit(rest_id):
    rest = query_one("SELECT * FROM driver_rests WHERE id = ?", (rest_id,))
    if rest is None:
        abort(404)
    driver = query_one("SELECT * FROM drivers WHERE id = ?", (rest["driver_id"],))

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        start = _to_date(request.form.get("start_date"))
        end = _to_date(request.form.get("end_date"))
        if not start or not end or end < start:
            flash("Ingresa fechas válidas (la fecha de fin no puede ser anterior a la de inicio).", "error")
            return render_template("descansos/form.html", **_form_context(
                "edit", driver=driver, rest=request.form, rest_id=rest_id
            ))
        days_count = (end - start).days + 1
        notes = request.form.get("notes", "").strip()
        execute(
            "UPDATE driver_rests SET start_date=?, end_date=?, days_count=?, notes=? WHERE id=?",
            (request.form.get("start_date"), request.form.get("end_date"), days_count, notes, rest_id),
        )
        # 22 sep, registro de actividad (ver app/audit.py).
        log_activity(
            "descansos", "EDITAR",
            f"Descanso de {driver['name']}: {request.form.get('start_date')} a {request.form.get('end_date')} ({days_count} día(s))",
            entity_type="descanso", entity_id=rest_id,
        )
        flash("Descanso actualizado.", "success")
        return redirect(url_for("descansos.list_view", driver_id=rest["driver_id"]))

    return render_template("descansos/form.html", **_form_context("edit", driver=driver, rest=rest, rest_id=rest_id))


@bp.route("/<int:rest_id>/eliminar", methods=["POST"])
# 29 sep, pedido de Braulio ("que solo el administrador pueda borrar...
# jornada laboral"): antes bastaba con "edit" -- ahora requiere "delete",
# que por defecto solo tiene Administrador (ver PERMISSIONS en
# app/auth.py y el comentario en app/permissions_catalog.py).
@permission_required("descansos", "delete")
def delete(rest_id):
    if not validate_csrf():
        abort(400)
    rest = query_one("SELECT * FROM driver_rests WHERE id = ?", (rest_id,))
    if rest is None:
        abort(404)
    driver = query_one("SELECT name FROM drivers WHERE id = ?", (rest["driver_id"],))
    execute("DELETE FROM driver_rests WHERE id = ?", (rest_id,))
    # 22 sep, registro de actividad (ver app/audit.py).
    log_activity(
        "descansos", "ELIMINAR",
        f"Descanso de {driver['name'] if driver else rest['driver_id']}: {rest['start_date']} a {rest['end_date']}",
        entity_type="descanso", entity_id=rest_id,
    )
    flash("Descanso eliminado.", "success")
    return redirect(url_for("descansos.list_view", driver_id=rest["driver_id"]))


# --- Importación masiva desde Excel (29 sep, pedido de Braulio: "creame un
# excel que se pueda usar e implementar la opcion de importarlo para subir
# de manera masiva descansos") -- mismo motor genérico que ya usan
# Flota/Conductores/Rutas/Honorarios, ver app/bulk_import.py. ---

@bp.route("/importar/plantilla")
@permission_required("descansos", "edit")
def import_template():
    buffer = build_import_template("Jornada laboral — Descansos", DRIVER_REST_COLUMNS, DRIVER_REST_EXAMPLE)
    return Response(
        buffer.getvalue(),
        mimetype=XLSX_MIME,
        headers={"Content-Disposition": 'attachment; filename="plantilla_descansos.xlsx"'},
    )


def _find_driver_for_row(row):
    """Busca el conductor de la fila primero por DNI (más preciso si hay
    nombres parecidos) y, si no se dio o no coincide, por nombre exacto
    (sin distinguir mayúsculas) -- mismo criterio que
    HONORARIOS_TEMPLATE_COLUMNS en app/bulk_import.py. No se filtra por
    conductores activos: un descanso histórico de alguien ya dado de baja
    sigue siendo un dato válido para la bitácora."""
    document_number = (row.get("driver_document") or "").strip()
    if document_number:
        driver = query_one("SELECT * FROM drivers WHERE document_number = ?", (document_number,))
        if driver:
            return driver
    name = (row.get("driver_name") or "").strip()
    if name:
        return query_one("SELECT * FROM drivers WHERE lower(name) = lower(?)", (name,))
    return None


def _apply_driver_rest_import(rows, example_skips):
    """Igual que los demás `_apply_..._import()`: NO repite la advertencia
    de "descanso más corto de lo que tocaría" que sí tiene new() -- calcular
    esa advertencia correctamente requeriría procesar las filas del archivo
    en orden cronológico por conductor (no en el orden en que vengan en el
    Excel) y no aporta nada a los datos guardados; el Panel/lista de Jornada
    laboral ya recalculan el estado de cumplimiento solos con lo que quede
    guardado, así que cualquier incumplimiento se sigue viendo igual
    después de importar."""
    created, errors = 0, []
    skipped = [
        {"row": r, "message": "Fila de ejemplo de la plantilla; se omitió automáticamente."}
        for r in example_skips
    ]
    seen = set()
    for row in rows:
        n = row["_row_number"]
        for warn in row["_warnings"]:
            errors.append({"row": n, "message": warn})

        driver = _find_driver_for_row(row)
        if driver is None:
            label = (row.get("driver_document") or "").strip() or (row.get("driver_name") or "").strip() or "(sin datos)"
            errors.append({"row": n, "message": f"No se encontró ningún conductor con \"{label}\"; la fila no se importó."})
            continue

        start = row.get("start_date")
        end = row.get("end_date")
        if not start or not end:
            errors.append({"row": n, "message": "Falta la fecha de inicio o de fin; la fila no se importó."})
            continue
        if end < start:
            errors.append({"row": n, "message": "La fecha de fin no puede ser anterior a la de inicio; la fila no se importó."})
            continue

        dedup_key = (driver["id"], start, end)
        if dedup_key in seen:
            skipped.append({"row": n, "message": f"{driver['name']} ya tiene esta misma fila repetida dentro del archivo; ya se había importado antes."})
            continue
        existing = query_one(
            "SELECT id FROM driver_rests WHERE driver_id = ? AND start_date = ? AND end_date = ?",
            (driver["id"], start, end),
        )
        if existing:
            skipped.append({"row": n, "message": f"{driver['name']} ya tiene un descanso registrado del {start} al {end}; no se duplicó."})
            continue
        seen.add(dedup_key)

        days_count = (dt.datetime.strptime(end, "%Y-%m-%d").date() - dt.datetime.strptime(start, "%Y-%m-%d").date()).days + 1
        notes = (row.get("notes") or "").strip()
        rest_id = execute(
            "INSERT INTO driver_rests (driver_id, start_date, end_date, days_count, notes) VALUES (?, ?, ?, ?, ?)",
            (driver["id"], start, end, days_count, notes),
        )
        log_activity(
            "descansos", "CREAR",
            f"Descanso de {driver['name']}: {start} a {end} ({days_count} día(s)) — importado desde Excel",
            entity_type="descanso", entity_id=rest_id,
        )
        created += 1
    return {"created": created, "updated": 0, "skipped": skipped, "errors": errors}


@bp.route("/importar", methods=["GET", "POST"])
@permission_required("descansos", "edit")
def import_rests():
    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        rows, file_error, example_skips = read_import_rows(request.files.get("file"), DRIVER_REST_COLUMNS, DRIVER_REST_EXAMPLE)
        if file_error:
            flash(file_error, "error")
            return redirect(url_for("descansos.import_rests"))
        result = _apply_driver_rest_import(rows, example_skips)
        return render_template(
            "import_result.html", result=result,
            back_url=url_for("descansos.list_view"), retry_url=url_for("descansos.import_rests"),
        )
    return render_template(
        "import_form.html", title="Importar descansos", module_label="los descansos ya tomados",
        template_url=url_for("descansos.import_template"), upload_url=url_for("descansos.import_rests"),
        back_url=url_for("descansos.list_view"), columns=DRIVER_REST_COLUMNS,
    )
