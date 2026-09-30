"""Módulo RRHH: viajes/liquidaciones ya cerradas y listas para enviarse a
Recursos Humanos.

10 sep, pedido de Braulio: "Hay que hacer un modulo nuevo de RRHH, en el
cual figuren los viajes con las liquidaciones que ya se cerraron y salen
con el OK y han sido enviadas a RRHH. Aca entran defrente las liquidaciones
que no tuvieron exceso de combustible, y las que si tuvieron se espera el
ok del administrador para que puedan figurar ahi. Dentro de este menu se
elige el periodo y figuran los viajes por cada conductor. Debe tener
tambien mas filtros."

Una liquidación queda "lista para RRHH" con el mismo criterio que ya usa
la columna RRHH del listado de Liquidaciones (ver
app/templates/liquidaciones/list.html, columna agregada en el patch 0020):
  - está cerrada (status = 'LIQUIDADO'), Y
  - o bien no tuvo exceso de combustible (fuel_excess nulo o 0), o bien un
    Administrador ya dio el OK explícito (rrhh_approved_at no nulo) — ver
    rrhh_approve() en app/routes/liquidaciones.py.

Este módulo es de SOLO LECTURA (un reporte) — el OK en sí se sigue dando
desde el detalle de la liquidación, no desde acá."""
from flask import Blueprint, Response, render_template, request

from app.accounting import office_choices
from app.auth import permission_required
from app.db import query_all, query_one
from app.helpers import today_str
from app.routes.viajes import ISSUER_CHOICES

bp = Blueprint("rrhh", __name__, url_prefix="/rrhh")

# Misma condición que la columna "RRHH" de liquidaciones/list.html: cerrada
# y, o sin exceso de combustible, o con el OK del Administrador ya dado.
READY_FOR_RRHH_SQL = (
    "a.status = 'LIQUIDADO' "
    "AND (a.fuel_excess IS NULL OR a.fuel_excess <= 0 OR a.rrhh_approved_at IS NOT NULL)"
)


def _ready_advances(month, driver_id, office, issuer, q):
    """Liquidaciones listas para RRHH en el periodo (mes) y filtros pedidos.

    30 sep, pedido de Braulio (liquidación separada del 2° conductor de un
    viaje doble conductor -- ver expense_advances.driver_id en schema.sql):
    el filtro/agrupación de conductor ahora mira al conductor DE LA PROPIA
    liquidación (a.driver_id), no siempre al principal del viaje
    (trips.driver_id) como antes -- así, si el 2° conductor ya tiene su
    propia liquidación cerrada, aparece en SU PROPIO panel en vez de
    mezclarse en el del 1°. d2/driver2_name se conservan solo para anotar
    "(+ 2° conductor)" en build_rrhh_workbook() cuando ese 2° conductor
    TODAVÍA no tiene su propia liquidación (dato de antes de este cambio)."""
    sql = f"""SELECT a.*, t.code as trip_code, t.origin, t.destination, t.issuer,
                     t.double_driver, t.driver2_id as trip_driver2_id,
                     d.name as driver_name, d2.name as driver2_name
              FROM expense_advances a
              JOIN trips t ON t.id = a.trip_id
              LEFT JOIN drivers d ON d.id = a.driver_id
              LEFT JOIN drivers d2 ON d2.id = t.driver2_id
              WHERE {READY_FOR_RRHH_SQL} AND strftime('%Y-%m', a.liquidated_at) = ?"""
    params = [month]
    if driver_id:
        sql += " AND a.driver_id = ?"
        params.append(driver_id)
    if office:
        sql += " AND a.office = ?"
        params.append(office)
    if issuer:
        sql += " AND t.issuer = ?"
        params.append(issuer)
    if q:
        # LOWER() en ambos lados (patch 0066) -- ver el comentario completo en
        # clientes.list_view().
        sql += " AND (LOWER(a.code) LIKE LOWER(?) OR LOWER(t.code) LIKE LOWER(?))"
        like = f"%{q}%"
        params.extend([like, like])
    sql += " ORDER BY d.name IS NULL, d.name, a.liquidated_at, a.id"
    rows = query_all(sql, params)
    # 30 sep: se convierte a dict (en vez de dejar los Row de sqlite3/
    # psycopg2, que no admiten agregar una clave nueva) para poder sumarle
    # "driver2_has_own_advance" -- lo usan tanto rrhh/list.html como
    # build_rrhh_workbook() para no anotar "(+ 2° conductor)" cuando ese 2°
    # conductor ya tiene su PROPIA liquidación (ver esa función).
    result = []
    for r in rows:
        row = dict(r)
        row["driver2_has_own_advance"] = _driver2_has_own_advance(row)
        result.append(row)
    return result


def _driver2_has_own_advance(row):
    """30 sep: ¿el 2° conductor de este viaje YA tiene su propia
    liquidación (esté o no lista para RRHH todavía)? Si es así,
    build_rrhh_workbook() no debe anotar "(+ 2° conductor)" en la fila del
    1° -- el 2° ya aparece (o va a aparecer) en su propio panel."""
    if not row["double_driver"] or not row["trip_driver2_id"] or row["trip_driver2_id"] == row["driver_id"]:
        return False
    return query_one(
        "SELECT 1 FROM expense_advances WHERE trip_id = ? AND driver_id = ?",
        (row["trip_id"], row["trip_driver2_id"]),
    ) is not None


def _group_by_driver(advances):
    """Agrupa las liquidaciones ya filtradas por conductor principal, mismo
    patrón que viajes._commissions_by_driver — un conductor por panel, con
    sus viajes/liquidaciones listados debajo."""
    by_driver = {}
    order = []
    for a in advances:
        key = a["driver_id"] or 0
        if key not in by_driver:
            by_driver[key] = {
                "driver_name": a["driver_name"] or "Sin conductor asignado",
                "advances": [],
                "trip_count": 0,
                "total_spent": 0.0,
            }
            order.append(key)
        entry = by_driver[key]
        entry["advances"].append(a)
        entry["trip_count"] += 1
        entry["total_spent"] += a["liquidated_expenses_total"] or 0.0
    return [by_driver[key] for key in order]


def _filters_from_args(args):
    """Mismos 5 filtros para la vista y para el export a Excel, así ambos
    quedan siempre de acuerdo (exportar respeta lo que se está viendo en
    pantalla)."""
    month = args.get("month") or today_str()[:7]
    driver_id = args.get("driver_id", type=int)
    office = args.get("office", "")
    issuer = args.get("issuer", "")
    q = args.get("q", "").strip()
    return month, driver_id, office, issuer, q


@bp.route("")
@permission_required("rrhh", "view")
def list_view():
    month, driver_id, office, issuer, q = _filters_from_args(request.args)

    advances = _ready_advances(month, driver_id, office, issuer, q)
    drivers = _group_by_driver(advances)
    grand_trips = len(advances)
    grand_total = sum(a["liquidated_expenses_total"] or 0.0 for a in advances)

    return render_template(
        "rrhh/list.html",
        month=month, driver_id=driver_id, office=office, issuer=issuer, q=q,
        drivers=drivers, grand_trips=grand_trips, grand_total=grand_total,
        all_drivers=query_all("SELECT id, name FROM drivers ORDER BY name"),
        offices=office_choices(), issuer_choices=ISSUER_CHOICES,
    )


# 10 sep, pedido de Braulio: "agregar la opcion de poder exportar a un
# cuadro de excel el detalle de viajes por mes" — mismo patrón que los
# demás exports del sistema (Historial de gastos, Comisiones por mes,
# Resumen contable): respeta los filtros que estén aplicados en ese momento
# en la pantalla (viene de los mismos query params del listado).
@bp.route("/exportar")
@permission_required("rrhh", "view")
def export_excel():
    from flask import current_app

    from app.reports import build_rrhh_workbook

    month, driver_id, office, issuer, q = _filters_from_args(request.args)

    advances = _ready_advances(month, driver_id, office, issuer, q)
    drivers = _group_by_driver(advances)

    buffer = build_rrhh_workbook(drivers, company_name=current_app.config["COMPANY_NAME"], month=month)
    filename = f"rrhh_{month}.xlsx"
    return Response(
        buffer.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
