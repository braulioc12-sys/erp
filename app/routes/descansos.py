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

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app.auth import permission_required, validate_csrf
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

        execute(
            "INSERT INTO driver_rests (driver_id, start_date, end_date, days_count, notes) VALUES (?, ?, ?, ?, ?)",
            (driver_id, request.form.get("start_date"), request.form.get("end_date"), days_count, notes),
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
        flash("Descanso actualizado.", "success")
        return redirect(url_for("descansos.list_view", driver_id=rest["driver_id"]))

    return render_template("descansos/form.html", **_form_context("edit", driver=driver, rest=rest, rest_id=rest_id))


@bp.route("/<int:rest_id>/eliminar", methods=["POST"])
@permission_required("descansos", "edit")
def delete(rest_id):
    if not validate_csrf():
        abort(400)
    rest = query_one("SELECT * FROM driver_rests WHERE id = ?", (rest_id,))
    if rest is None:
        abort(404)
    execute("DELETE FROM driver_rests WHERE id = ?", (rest_id,))
    flash("Descanso eliminado.", "success")
    return redirect(url_for("descansos.list_view", driver_id=rest["driver_id"]))
