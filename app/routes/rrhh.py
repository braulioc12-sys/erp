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
from flask import Blueprint, render_template, request

from app.accounting import office_choices
from app.auth import permission_required
from app.db import query_all
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
    El filtro de conductor mira solo al conductor PRINCIPAL del viaje
    (trips.driver_id) — una liquidación es una sola por viaje, no se separa
    por conductor como sí pasa con las comisiones (ver
    viajes._commissions_by_driver); en un viaje de doble conductor el
    segundo conductor se sigue mostrando junto al primero en cada fila."""
    sql = f"""SELECT a.*, t.code as trip_code, t.origin, t.destination, t.issuer,
                     t.double_driver, d.id as driver_id, d.name as driver_name,
                     d2.name as driver2_name
              FROM expense_advances a
              JOIN trips t ON t.id = a.trip_id
              LEFT JOIN drivers d ON d.id = t.driver_id
              LEFT JOIN drivers d2 ON d2.id = t.driver2_id
              WHERE {READY_FOR_RRHH_SQL} AND strftime('%Y-%m', a.liquidated_at) = ?"""
    params = [month]
    if driver_id:
        sql += " AND t.driver_id = ?"
        params.append(driver_id)
    if office:
        sql += " AND a.office = ?"
        params.append(office)
    if issuer:
        sql += " AND t.issuer = ?"
        params.append(issuer)
    if q:
        sql += " AND (a.code LIKE ? OR t.code LIKE ?)"
        like = f"%{q}%"
        params.extend([like, like])
    sql += " ORDER BY d.name IS NULL, d.name, a.liquidated_at, a.id"
    return query_all(sql, params)


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


@bp.route("")
@permission_required("rrhh", "view")
def list_view():
    month = request.args.get("month") or today_str()[:7]
    driver_id = request.args.get("driver_id", type=int)
    office = request.args.get("office", "")
    issuer = request.args.get("issuer", "")
    q = request.args.get("q", "").strip()

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
