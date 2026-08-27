"""Control de neumáticos por unidad: posición (con diagrama según tipo de
unidad — tracto, carreta o camión), marca, fecha de instalación y
kilometraje acumulado.

El kilometraje acumulado de cada llanta NO se guarda como un contador
aparte: se calcula comparando el kilometraje actual de la unidad
(vehicles.current_km) contra el kilometraje que tenía la unidad cuando se
instaló esa llanta (km_at_install). Como current_km ya se actualiza solo
con el movimiento real del vehículo (al registrar un mantenimiento, editar
la unidad en Flota, o sincronizar GPS — ver esos módulos), el acumulado de
cada llanta queda al día automáticamente sin ningún trabajo extra.
"""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app.auth import permission_required, validate_csrf
from app.db import execute, query_all, query_one
from app.helpers import parse_date, parse_float, today_str
from app.tire_positions import (
    DEFAULT_EXPECTED_LIFE_KM,
    VEHICLE_TYPE_LABELS,
    get_axle_ys,
    get_diagram_height,
    get_position_label,
    get_positions,
)

bp = Blueprint("neumaticos", __name__, url_prefix="/neumaticos")

# Umbrales de alerta sobre el % de vida útil consumida.
WARN_THRESHOLD_PCT = 80
DANGER_THRESHOLD_PCT = 100
# A partir de qué % se avisa en el Panel (dashboard).
DASHBOARD_ALERT_PCT = 90


def _tire_metrics(tire, vehicle_current_km):
    """Devuelve (accumulated_km, percent, status_class, badge_class) para
    una llanta ACTIVO, según el kilometraje actual de su unidad."""
    if vehicle_current_km is None:
        return None, None, "tire-ok", "badge-activo"
    accumulated = max(vehicle_current_km - tire["km_at_install"], 0)
    expected = tire["expected_life_km"] or DEFAULT_EXPECTED_LIFE_KM
    percent = round(accumulated / expected * 100) if expected else None
    if percent is None:
        status_class, badge_class = "tire-ok", "badge-activo"
    elif percent >= DANGER_THRESHOLD_PCT:
        status_class, badge_class = "tire-danger", "badge-vencida"
    elif percent >= WARN_THRESHOLD_PCT:
        status_class, badge_class = "tire-warn", "badge-mantenimiento"
    else:
        status_class, badge_class = "tire-ok", "badge-activo"
    return accumulated, percent, status_class, badge_class


def _get_vehicle_or_404(vehicle_id):
    vehicle = query_one("SELECT * FROM vehicles WHERE id = ?", (vehicle_id,))
    if vehicle is None:
        abort(404)
    return vehicle


def tire_alerts():
    """Llantas activas al DASHBOARD_ALERT_PCT% o más de su vida útil
    estimada, para mostrar en el Panel."""
    rows = query_all(
        """SELECT t.*, v.plate AS vehicle_plate, v.current_km AS vehicle_current_km,
                  v.vehicle_type AS vehicle_type
           FROM tires t JOIN vehicles v ON v.id = t.vehicle_id
           WHERE t.status = 'ACTIVO'"""
    )
    alerts = []
    for r in rows:
        accumulated, percent, _, _ = _tire_metrics(r, r["vehicle_current_km"])
        if percent is not None and percent >= DASHBOARD_ALERT_PCT:
            alerts.append(
                {
                    "plate": r["vehicle_plate"],
                    "position_label": get_position_label(r["vehicle_type"], r["position_code"]),
                    "percent": percent,
                    "accumulated_km": accumulated,
                    "expected_life_km": r["expected_life_km"],
                    "overdue": percent >= DANGER_THRESHOLD_PCT,
                }
            )
    alerts.sort(key=lambda a: a["percent"], reverse=True)
    return alerts


@bp.route("")
@permission_required("mantenimiento", "view")
def list_view():
    vehicles = query_all("SELECT * FROM vehicles ORDER BY plate")
    summary = []
    for v in vehicles:
        positions = get_positions(v["vehicle_type"])
        active_tires = query_all(
            "SELECT * FROM tires WHERE vehicle_id = ? AND status = 'ACTIVO'", (v["id"],)
        )
        worst_badge = "badge-activo"
        for t in active_tires:
            _, _, _, badge_class = _tire_metrics(t, v["current_km"])
            if badge_class == "badge-vencida":
                worst_badge = "badge-vencida"
                break
            if badge_class == "badge-mantenimiento":
                worst_badge = "badge-mantenimiento"
        summary.append(
            {
                "vehicle": v,
                "type_label": VEHICLE_TYPE_LABELS.get(v["vehicle_type"], v["vehicle_type"]),
                "total_positions": len(positions),
                "installed": len(active_tires),
                "worst_badge": worst_badge,
            }
        )
    return render_template("neumaticos/list.html", summary=summary)


@bp.route("/unidad/<int:vehicle_id>")
@permission_required("mantenimiento", "view")
def diagram(vehicle_id):
    vehicle = _get_vehicle_or_404(vehicle_id)
    positions = get_positions(vehicle["vehicle_type"])
    active_tires = query_all(
        "SELECT * FROM tires WHERE vehicle_id = ? AND status = 'ACTIVO'", (vehicle_id,)
    )
    tires_by_position = {t["position_code"]: t for t in active_tires}

    rows = []
    for p in positions:
        tire = tires_by_position.get(p["code"])
        row = {"position": p}
        if tire:
            accumulated, percent, status_class, badge_class = _tire_metrics(tire, vehicle["current_km"])
            row.update(
                {
                    "tire": tire,
                    "accumulated_km": accumulated,
                    "percent": percent,
                    "status_class": status_class,
                    "badge_class": badge_class,
                }
            )
        else:
            row.update({"tire": None, "status_class": "tire-empty"})
        rows.append(row)

    retired = query_all(
        """SELECT * FROM tires WHERE vehicle_id = ? AND status = 'RETIRADO'
           ORDER BY removed_date DESC, id DESC""",
        (vehicle_id,),
    )
    retired_rows = [
        {"tire": t, "position_label": get_position_label(vehicle["vehicle_type"], t["position_code"])}
        for t in retired
    ]

    return render_template(
        "neumaticos/diagram.html",
        vehicle=vehicle,
        type_label=VEHICLE_TYPE_LABELS.get(vehicle["vehicle_type"], vehicle["vehicle_type"]),
        rows=rows,
        retired_rows=retired_rows,
        axle_ys=get_axle_ys(vehicle["vehicle_type"]),
        diagram_height=get_diagram_height(vehicle["vehicle_type"]),
        vehicle_type=vehicle["vehicle_type"],
    )


@bp.route("/unidad/<int:vehicle_id>/posicion/<position_code>/nueva", methods=["GET", "POST"])
@permission_required("mantenimiento", "edit")
def new_tire(vehicle_id, position_code):
    vehicle = _get_vehicle_or_404(vehicle_id)
    valid_codes = {p["code"] for p in get_positions(vehicle["vehicle_type"])}
    if position_code not in valid_codes:
        abort(404)
    existing = query_one(
        "SELECT id FROM tires WHERE vehicle_id = ? AND position_code = ? AND status = 'ACTIVO'",
        (vehicle_id, position_code),
    )
    if existing:
        flash("Esa posición ya tiene una llanta activa. Usa 'Reemplazar' desde su detalle.", "error")
        return redirect(url_for("neumaticos.diagram", vehicle_id=vehicle_id))

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        brand = request.form.get("brand", "").strip()
        install_date = parse_date(request.form.get("install_date")) or today_str()
        km_at_install = parse_float(request.form.get("km_at_install"), vehicle["current_km"] or 0)
        expected_life_km = parse_float(request.form.get("expected_life_km"), DEFAULT_EXPECTED_LIFE_KM)
        notes = request.form.get("notes", "").strip()
        execute(
            """INSERT INTO tires (vehicle_id, position_code, brand, install_date, km_at_install,
               expected_life_km, notes) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (vehicle_id, position_code, brand or None, install_date, km_at_install, expected_life_km, notes or None),
        )
        flash("Llanta registrada.", "success")
        return redirect(url_for("neumaticos.diagram", vehicle_id=vehicle_id))

    return render_template(
        "neumaticos/tire_form.html",
        vehicle=vehicle,
        position_label=get_position_label(vehicle["vehicle_type"], position_code),
        mode="new",
        tire=None,
        today=today_str(),
        default_expected_life_km=DEFAULT_EXPECTED_LIFE_KM,
    )


@bp.route("/llanta/<int:tire_id>")
@permission_required("mantenimiento", "view")
def detail(tire_id):
    tire = query_one("SELECT * FROM tires WHERE id = ?", (tire_id,))
    if tire is None:
        abort(404)
    vehicle = _get_vehicle_or_404(tire["vehicle_id"])
    accumulated, percent, status_class, badge_class = (None, None, None, None)
    if tire["status"] == "ACTIVO":
        accumulated, percent, status_class, badge_class = _tire_metrics(tire, vehicle["current_km"])

    history = query_all(
        """SELECT * FROM tires WHERE vehicle_id = ? AND position_code = ? AND id != ?
           ORDER BY install_date DESC, id DESC""",
        (tire["vehicle_id"], tire["position_code"], tire_id),
    )

    return render_template(
        "neumaticos/tire_detail.html",
        tire=tire,
        vehicle=vehicle,
        position_label=get_position_label(vehicle["vehicle_type"], tire["position_code"]),
        accumulated_km=accumulated,
        percent=percent,
        badge_class=badge_class,
        history=history,
    )


@bp.route("/llanta/<int:tire_id>/reemplazar", methods=["GET", "POST"])
@permission_required("mantenimiento", "edit")
def replace_tire(tire_id):
    old_tire = query_one("SELECT * FROM tires WHERE id = ?", (tire_id,))
    if old_tire is None:
        abort(404)
    if old_tire["status"] != "ACTIVO":
        flash("Esta llanta ya fue retirada.", "error")
        return redirect(url_for("neumaticos.detail", tire_id=tire_id))
    vehicle = _get_vehicle_or_404(old_tire["vehicle_id"])

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        removed_date = parse_date(request.form.get("removed_date")) or today_str()
        removed_km = parse_float(request.form.get("removed_km"), vehicle["current_km"] or 0)
        removal_reason = request.form.get("removal_reason", "").strip()
        execute(
            """UPDATE tires SET status = 'RETIRADO', removed_date = ?, removed_km = ?,
               removal_reason = ? WHERE id = ?""",
            (removed_date, removed_km, removal_reason or None, tire_id),
        )
        brand = request.form.get("new_brand", "").strip()
        install_date = parse_date(request.form.get("new_install_date")) or today_str()
        km_at_install = parse_float(request.form.get("new_km_at_install"), removed_km)
        expected_life_km = parse_float(request.form.get("new_expected_life_km"), DEFAULT_EXPECTED_LIFE_KM)
        notes = request.form.get("new_notes", "").strip()
        execute(
            """INSERT INTO tires (vehicle_id, position_code, brand, install_date, km_at_install,
               expected_life_km, notes) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                old_tire["vehicle_id"], old_tire["position_code"], brand or None, install_date,
                km_at_install, expected_life_km, notes or None,
            ),
        )
        flash("Llanta reemplazada. Se guardó el historial de la anterior.", "success")
        return redirect(url_for("neumaticos.diagram", vehicle_id=old_tire["vehicle_id"]))

    return render_template(
        "neumaticos/tire_form.html",
        vehicle=vehicle,
        position_label=get_position_label(vehicle["vehicle_type"], old_tire["position_code"]),
        mode="replace",
        tire=old_tire,
        today=today_str(),
        default_expected_life_km=DEFAULT_EXPECTED_LIFE_KM,
    )


@bp.route("/llanta/<int:tire_id>/retirar", methods=["POST"])
@permission_required("mantenimiento", "edit")
def retire_tire(tire_id):
    if not validate_csrf():
        abort(400)
    tire = query_one("SELECT * FROM tires WHERE id = ?", (tire_id,))
    if tire is None:
        abort(404)
    if tire["status"] != "ACTIVO":
        flash("Esta llanta ya fue retirada.", "error")
        return redirect(url_for("neumaticos.detail", tire_id=tire_id))
    vehicle = _get_vehicle_or_404(tire["vehicle_id"])
    execute(
        """UPDATE tires SET status = 'RETIRADO', removed_date = ?, removed_km = ?,
           removal_reason = ? WHERE id = ?""",
        (today_str(), vehicle["current_km"] or tire["km_at_install"], "Retirada sin reemplazo inmediato", tire_id),
    )
    flash("Llanta retirada. La posición queda disponible en el diagrama.", "success")
    return redirect(url_for("neumaticos.diagram", vehicle_id=tire["vehicle_id"]))
