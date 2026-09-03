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
from app.db import execute, get_db, query_all, query_one
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

# Bandas de vida útil (2 sep, pedido de Braulio): en vez de un solo % fijo,
# cada llanta pasa por 4 estados según su kilometraje acumulado comparado
# contra SU PROPIA vida útil estimada (expected_life_km, configurable al
# registrarla — algunas marcas/modelos duran más que otras). Los tercios de
# esa vida útil son los cortes de cada banda: con el valor por defecto de
# 60,000 km (DEFAULT_EXPECTED_LIFE_KM) esto da exactamente Bueno 0-20,000 /
# Regular 20,000-40,000 / Grave 40,000-60,000 / Alerta más de 60,000 —
# pero una llanta configurada con otra vida útil usa esos mismos tercios
# sobre SU número.
LIFE_STAGES = ("BUENO", "REGULAR", "GRAVE", "ALERTA")
LIFE_STAGE_LABELS = {
    "BUENO": "Bueno",
    "REGULAR": "Regular",
    "GRAVE": "Grave",
    "ALERTA": "Alerta: debe cambiarse",
}
LIFE_STAGE_STATUS_CLASS = {"BUENO": "tire-ok", "REGULAR": "tire-warn", "GRAVE": "tire-grave", "ALERTA": "tire-danger"}
LIFE_STAGE_BADGE_CLASS = {
    "BUENO": "badge-activo", "REGULAR": "badge-mantenimiento", "GRAVE": "badge-grave", "ALERTA": "badge-vencida",
}
# A partir de qué banda se avisa en el Panel (dashboard) — Grave ya es un
# aviso temprano de que hay que ir programando el cambio; Alerta es el
# "ya debe cambiarse" explícito que pidió Braulio.
DASHBOARD_ALERT_STAGES = ("GRAVE", "ALERTA")

# Inventario de llantas por código (2 sep, pedido de Braulio): registro
# independiente del de repuestos, obligatorio antes de poder asignar una
# llanta a una unidad — ver tire_inventory en schema.sql.
INVENTORY_STATUS_LABELS = {
    "DISPONIBLE": "Disponible",
    "ASIGNADA": "Asignada",
    "RETIRADA": "Retirada",
}
INVENTORY_STATUS_BADGE_CLASS = {
    "DISPONIBLE": "badge-activo",
    "ASIGNADA": "badge-en_curso",
    "RETIRADA": "badge-inactivo",
}


def _life_stage(accumulated_km, expected_life_km):
    if expected_life_km is None or expected_life_km <= 0:
        return None
    third = expected_life_km / 3.0
    if accumulated_km > expected_life_km:
        return "ALERTA"
    if accumulated_km > 2 * third:
        return "GRAVE"
    if accumulated_km > third:
        return "REGULAR"
    return "BUENO"


def _tire_metrics(tire, vehicle_current_km):
    """Devuelve (accumulated_km, percent, stage, status_class, badge_class)
    para una llanta ACTIVO, según el kilometraje actual de su unidad.
    `percent` sigue siendo el % de vida útil consumida (para mostrarlo de
    referencia); `stage` es una de LIFE_STAGES."""
    if vehicle_current_km is None:
        return None, None, None, "tire-ok", "badge-activo"
    accumulated = max(vehicle_current_km - tire["km_at_install"], 0)
    expected = tire["expected_life_km"] or DEFAULT_EXPECTED_LIFE_KM
    percent = round(accumulated / expected * 100) if expected else None
    stage = _life_stage(accumulated, expected)
    if stage is None:
        status_class, badge_class = "tire-ok", "badge-activo"
    else:
        status_class = LIFE_STAGE_STATUS_CLASS[stage]
        badge_class = LIFE_STAGE_BADGE_CLASS[stage]
    return accumulated, percent, stage, status_class, badge_class


def _get_vehicle_or_404(vehicle_id):
    vehicle = query_one("SELECT * FROM vehicles WHERE id = ?", (vehicle_id,))
    if vehicle is None:
        abort(404)
    return vehicle


def _inventory_current_assignment(tire_inventory_id):
    """Si esta llanta de inventario está ASIGNADA, devuelve su fila ACTIVO
    actual en `tires` (con placa/posición) — o None si está DISPONIBLE o
    RETIRADA."""
    row = query_one(
        """SELECT t.*, v.plate AS vehicle_plate, v.vehicle_type AS vehicle_type
           FROM tires t JOIN vehicles v ON v.id = t.vehicle_id
           WHERE t.tire_inventory_id = ? AND t.status = 'ACTIVO'""",
        (tire_inventory_id,),
    )
    return row


def _available_inventory_tires():
    return query_all("SELECT * FROM tire_inventory WHERE status = 'DISPONIBLE' ORDER BY code")


def _move_destination_data():
    """Datos para el selector de 'se movió a otra unidad' en el formulario
    de retiro/reemplazo: `vehicles` (para el desplegable de unidad),
    `vehicles_data` (tipo y posiciones ya ocupadas de cada unidad, para
    filtrar en JS qué posiciones de destino ofrecer) y `positions_by_type`
    (posiciones válidas por tipo de unidad — solo hay 3 tipos, así que se
    manda completo en vez de resolverlo con una consulta por cada cambio
    de unidad en el desplegable)."""
    vehicles = query_all(
        "SELECT id, plate, vehicle_type, current_km FROM vehicles WHERE status != 'INACTIVO' ORDER BY plate"
    )
    active_tires = query_all("SELECT vehicle_id, position_code FROM tires WHERE status = 'ACTIVO'")
    occupied_by_vehicle = {}
    for row in active_tires:
        occupied_by_vehicle.setdefault(row["vehicle_id"], []).append(row["position_code"])
    vehicles_data = {
        str(v["id"]): {
            "plate": v["plate"],
            "type": v["vehicle_type"],
            "occupied": occupied_by_vehicle.get(v["id"], []),
        }
        for v in vehicles
    }
    positions_by_type = {
        vt: [{"code": p["code"], "label": p["label"]} for p in get_positions(vt)] for vt in VEHICLE_TYPE_LABELS
    }
    return vehicles, vehicles_data, positions_by_type


def _tire_journey(tire):
    """Devuelve la cadena completa de esta llanta física a través de las
    distintas unidades/posiciones por las que pasó: cada 'reemplazo' o
    'movida a otra unidad' crea una fila nueva enlazada a la anterior por
    `moved_to_tire_id`. Camina hacia atrás (¿alguna fila apunta a esta como
    su destino?) y hacia adelante (`moved_to_tire_id` de esta fila) hasta
    los dos extremos, devolviendo la lista completa en orden cronológico.
    Si la llanta nunca se movió, devuelve solo `[tire]`."""
    chain = [tire]
    current = tire
    while True:
        prev = query_one("SELECT * FROM tires WHERE moved_to_tire_id = ?", (current["id"],))
        if prev is None:
            break
        chain.insert(0, prev)
        current = prev
    current = tire
    while current["moved_to_tire_id"]:
        nxt = query_one("SELECT * FROM tires WHERE id = ?", (current["moved_to_tire_id"],))
        if nxt is None:
            break
        chain.append(nxt)
        current = nxt
    return chain


def _journey_rows(chain, current_tire_id):
    rows = []
    for t in chain:
        v = query_one("SELECT plate, vehicle_type FROM vehicles WHERE id = ?", (t["vehicle_id"],))
        rows.append(
            {
                "tire": t,
                "plate": v["plate"] if v else "—",
                "position_label": get_position_label(v["vehicle_type"], t["position_code"]) if v else t["position_code"],
                "is_current": t["id"] == current_tire_id,
            }
        )
    return rows


def _apply_disposition(old_tire, vehicle, removed_date, removed_km, removal_reason):
    """Aplica qué pasó con `old_tire` al retirarla — llamado desde
    replace_tire y retire_tire, que ya validaron fecha/km de retiro. Marca
    `old_tire` como RETIRADO y, según el campo `disposition` del
    formulario ('DESCARTADA' o 'MOVIDA'), la deja así o crea su fila nueva
    en la unidad/posición de destino elegida, enlazando ambas filas vía
    `moved_to_tire_id`. Devuelve True si aplicó bien (ya hizo commit y
    mostró el flash de éxito); False si hubo un error de validación (ya
    mostró el flash de error, no tocó la base de datos — el llamador debe
    volver a mostrar el formulario)."""
    disposition = request.form.get("disposition", "").strip()
    if disposition not in ("DESCARTADA", "MOVIDA"):
        flash("Indica qué pasó con la llanta retirada: si se descartó o si se movió a otra unidad.", "error")
        return False

    if disposition == "DESCARTADA":
        db = get_db()
        db.execute(
            """UPDATE tires SET status = 'RETIRADO', removed_date = ?, removed_km = ?,
               removal_reason = ?, disposition = 'DESCARTADA' WHERE id = ?""",
            (removed_date, removed_km, removal_reason or None, old_tire["id"]),
        )
        if old_tire["tire_inventory_id"]:
            db.execute(
                "UPDATE tire_inventory SET status = 'RETIRADA' WHERE id = ?",
                (old_tire["tire_inventory_id"],),
            )
        db.commit()
        flash("Llanta retirada y marcada como descartada.", "success")
        return True

    # MOVIDA a otra unidad.
    dest_vehicle_id = request.form.get("dest_vehicle_id", "").strip()
    dest_vehicle = query_one("SELECT * FROM vehicles WHERE id = ?", (dest_vehicle_id,)) if dest_vehicle_id else None
    if dest_vehicle is None:
        flash("Selecciona a qué unidad se movió la llanta.", "error")
        return False
    dest_position_code = request.form.get("dest_position_code", "").strip()
    valid_codes = {p["code"] for p in get_positions(dest_vehicle["vehicle_type"])}
    if dest_position_code not in valid_codes:
        flash("Selecciona una posición válida en la unidad de destino.", "error")
        return False
    occupied = query_one(
        "SELECT id FROM tires WHERE vehicle_id = ? AND position_code = ? AND status = 'ACTIVO'",
        (dest_vehicle["id"], dest_position_code),
    )
    if occupied:
        flash(f"La posición elegida en {dest_vehicle['plate']} ya tiene una llanta activa — elige otra.", "error")
        return False

    # El km_at_install de la fila nueva se calcula para que el acumulado de
    # desgaste siga contando desde donde se quedó la llanta, en vez de
    # reiniciarse en 0 con el odómetro de la unidad de destino (que es otro
    # vehículo, con su propio kilometraje).
    accumulated_at_removal = max(removed_km - old_tire["km_at_install"], 0)
    dest_install_date = parse_date(request.form.get("dest_install_date")) or removed_date
    dest_km_at_install = (dest_vehicle["current_km"] or 0) - accumulated_at_removal
    dest_expected_life_km = parse_float(request.form.get("dest_expected_life_km"), old_tire["expected_life_km"])
    dest_notes = request.form.get("dest_notes", "").strip()

    db = get_db()
    cur = db.execute(
        """INSERT INTO tires (vehicle_id, position_code, brand, install_date, km_at_install,
           expected_life_km, notes, tire_inventory_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            dest_vehicle["id"], dest_position_code, old_tire["brand"], dest_install_date,
            dest_km_at_install, dest_expected_life_km, dest_notes or None, old_tire["tire_inventory_id"],
        ),
    )
    new_tire_id = cur.lastrowid
    db.execute(
        """UPDATE tires SET status = 'RETIRADO', removed_date = ?, removed_km = ?,
           removal_reason = ?, disposition = 'MOVIDA', moved_to_tire_id = ? WHERE id = ?""",
        (removed_date, removed_km, removal_reason or None, new_tire_id, old_tire["id"]),
    )
    db.commit()
    flash(
        f"Llanta retirada y movida a {dest_vehicle['plate']} "
        f"({get_position_label(dest_vehicle['vehicle_type'], dest_position_code)}).",
        "success",
    )
    return True


def tire_alerts():
    """Llantas activas en banda Grave o Alerta (ver DASHBOARD_ALERT_STAGES),
    para mostrar en el Panel."""
    rows = query_all(
        """SELECT t.*, v.plate AS vehicle_plate, v.current_km AS vehicle_current_km,
                  v.vehicle_type AS vehicle_type
           FROM tires t JOIN vehicles v ON v.id = t.vehicle_id
           WHERE t.status = 'ACTIVO'"""
    )
    alerts = []
    for r in rows:
        accumulated, percent, stage, _, _ = _tire_metrics(r, r["vehicle_current_km"])
        if stage in DASHBOARD_ALERT_STAGES:
            alerts.append(
                {
                    "plate": r["vehicle_plate"],
                    "position_label": get_position_label(r["vehicle_type"], r["position_code"]),
                    "percent": percent,
                    "stage": stage,
                    "stage_label": LIFE_STAGE_LABELS[stage],
                    "accumulated_km": accumulated,
                    "expected_life_km": r["expected_life_km"],
                    "overdue": stage == "ALERTA",
                }
            )
    alerts.sort(key=lambda a: a["percent"], reverse=True)
    return alerts


@bp.route("")
@permission_required("neumaticos", "view")
def list_view():
    vehicles = query_all("SELECT * FROM vehicles ORDER BY plate")
    summary = []
    for v in vehicles:
        positions = get_positions(v["vehicle_type"])
        active_tires = query_all(
            "SELECT * FROM tires WHERE vehicle_id = ? AND status = 'ACTIVO'", (v["id"],)
        )
        # Prioridad de gravedad para mostrar el peor caso de la unidad:
        # Alerta > Grave > Regular > Bueno.
        badge_priority = {"badge-vencida": 3, "badge-grave": 2, "badge-mantenimiento": 1, "badge-activo": 0}
        worst_badge = "badge-activo"
        for t in active_tires:
            _, _, _, _, badge_class = _tire_metrics(t, v["current_km"])
            if badge_priority.get(badge_class, 0) > badge_priority.get(worst_badge, 0):
                worst_badge = badge_class
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


@bp.route("/inventario")
@permission_required("neumaticos", "view")
def inventory_list():
    tires = query_all("SELECT * FROM tire_inventory ORDER BY code")
    rows = []
    for t in tires:
        assignment = None
        if t["status"] == "ASIGNADA":
            assignment = _inventory_current_assignment(t["id"])
        rows.append(
            {
                "tire": t,
                "status_label": INVENTORY_STATUS_LABELS.get(t["status"], t["status"]),
                "badge_class": INVENTORY_STATUS_BADGE_CLASS.get(t["status"], "badge-activo"),
                "assignment": assignment,
                "position_label": (
                    get_position_label(assignment["vehicle_type"], assignment["position_code"])
                    if assignment else None
                ),
            }
        )
    return render_template("neumaticos/inventory_list.html", rows=rows)


@bp.route("/inventario/nueva", methods=["GET", "POST"])
@permission_required("neumaticos", "edit")
def inventory_new():
    next_url = request.args.get("next") or request.form.get("next") or ""
    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        code = request.form.get("code", "").strip()
        brand = request.form.get("brand", "").strip()
        expected_life_km = parse_float(request.form.get("expected_life_km"), DEFAULT_EXPECTED_LIFE_KM)
        notes = request.form.get("notes", "").strip()
        if not code:
            flash("Ingresa el código de la llanta.", "error")
        else:
            existing = query_one("SELECT id FROM tire_inventory WHERE code = ?", (code,))
            if existing:
                flash(f'Ya existe una llanta registrada con el código "{code}".', "error")
            else:
                execute(
                    """INSERT INTO tire_inventory (code, brand, expected_life_km, notes)
                       VALUES (?, ?, ?, ?)""",
                    (code, brand or None, expected_life_km, notes or None),
                )
                flash(f'Llanta "{code}" registrada en el inventario.', "success")
                return redirect(next_url or url_for("neumaticos.inventory_list"))
        return render_template(
            "neumaticos/inventory_form.html",
            default_expected_life_km=DEFAULT_EXPECTED_LIFE_KM,
            next_url=next_url,
            form=request.form,
        )

    return render_template(
        "neumaticos/inventory_form.html",
        default_expected_life_km=DEFAULT_EXPECTED_LIFE_KM,
        next_url=next_url,
        form=None,
    )


@bp.route("/unidad/<int:vehicle_id>")
@permission_required("neumaticos", "view")
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
            accumulated, percent, stage, status_class, badge_class = _tire_metrics(tire, vehicle["current_km"])
            row.update(
                {
                    "tire": tire,
                    "accumulated_km": accumulated,
                    "percent": percent,
                    "stage": stage,
                    "stage_label": LIFE_STAGE_LABELS.get(stage),
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
    retired_rows = []
    for t in retired:
        moved_to_plate = None
        if t["disposition"] == "MOVIDA" and t["moved_to_tire_id"]:
            dest = query_one(
                """SELECT v.plate, t2.position_code, v.vehicle_type FROM tires t2
                   JOIN vehicles v ON v.id = t2.vehicle_id WHERE t2.id = ?""",
                (t["moved_to_tire_id"],),
            )
            if dest:
                moved_to_plate = f"{dest['plate']} ({get_position_label(dest['vehicle_type'], dest['position_code'])})"
        retired_rows.append(
            {
                "tire": t,
                "position_label": get_position_label(vehicle["vehicle_type"], t["position_code"]),
                "moved_to_plate": moved_to_plate,
            }
        )

    rotations = query_all(
        "SELECT * FROM tire_rotations WHERE vehicle_id = ? ORDER BY rotation_date DESC, id DESC",
        (vehicle_id,),
    )
    rotation_rows = []
    for rot in rotations:
        moves = query_all(
            """SELECT trm.*, t.brand AS tire_brand FROM tire_rotation_moves trm
               JOIN tires t ON t.id = trm.tire_id
               WHERE trm.rotation_id = ? ORDER BY trm.id""",
            (rot["id"],),
        )
        rotation_rows.append(
            {
                "rotation": rot,
                "moves": [
                    {
                        "tire_brand": mv["tire_brand"],
                        "from_label": get_position_label(vehicle["vehicle_type"], mv["from_position_code"]),
                        "to_label": get_position_label(vehicle["vehicle_type"], mv["to_position_code"]),
                    }
                    for mv in moves
                ],
            }
        )

    return render_template(
        "neumaticos/diagram.html",
        vehicle=vehicle,
        type_label=VEHICLE_TYPE_LABELS.get(vehicle["vehicle_type"], vehicle["vehicle_type"]),
        rows=rows,
        retired_rows=retired_rows,
        rotation_rows=rotation_rows,
        active_tire_count=len(active_tires),
        axle_ys=get_axle_ys(vehicle["vehicle_type"]),
        diagram_height=get_diagram_height(vehicle["vehicle_type"]),
        vehicle_type=vehicle["vehicle_type"],
    )


@bp.route("/unidad/<int:vehicle_id>/posicion/<position_code>/nueva", methods=["GET", "POST"])
@permission_required("neumaticos", "edit")
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
        tire_inventory_id = parse_float(request.form.get("tire_inventory_id"), None)
        inv_tire = (
            query_one(
                "SELECT * FROM tire_inventory WHERE id = ? AND status = 'DISPONIBLE'",
                (int(tire_inventory_id),),
            )
            if tire_inventory_id
            else None
        )
        install_date = parse_date(request.form.get("install_date")) or today_str()
        km_at_install = parse_float(request.form.get("km_at_install"), vehicle["current_km"] or 0)
        expected_life_km = parse_float(
            request.form.get("expected_life_km"), inv_tire["expected_life_km"] if inv_tire else DEFAULT_EXPECTED_LIFE_KM
        )
        notes = request.form.get("notes", "").strip()

        if inv_tire is None:
            flash(
                "Selecciona una llanta disponible del inventario — primero debe registrarse "
                "con su código antes de poder asignarla a una unidad.",
                "error",
            )
        else:
            db = get_db()
            db.execute(
                """INSERT INTO tires (vehicle_id, position_code, brand, install_date, km_at_install,
                   expected_life_km, notes, tire_inventory_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    vehicle_id, position_code, inv_tire["brand"], install_date, km_at_install,
                    expected_life_km, notes or None, inv_tire["id"],
                ),
            )
            db.execute("UPDATE tire_inventory SET status = 'ASIGNADA' WHERE id = ?", (inv_tire["id"],))
            db.commit()
            flash(f'Llanta "{inv_tire["code"]}" asignada a esta posición.', "success")
            return redirect(url_for("neumaticos.diagram", vehicle_id=vehicle_id))

    return render_template(
        "neumaticos/tire_form.html",
        vehicle=vehicle,
        position_label=get_position_label(vehicle["vehicle_type"], position_code),
        mode="new",
        tire=None,
        today=today_str(),
        default_expected_life_km=DEFAULT_EXPECTED_LIFE_KM,
        available_tires=_available_inventory_tires(),
    )


@bp.route("/llanta/<int:tire_id>")
@permission_required("neumaticos", "view")
def detail(tire_id):
    tire = query_one("SELECT * FROM tires WHERE id = ?", (tire_id,))
    if tire is None:
        abort(404)
    vehicle = _get_vehicle_or_404(tire["vehicle_id"])
    accumulated, percent, stage, status_class, badge_class = (None, None, None, None, None)
    if tire["status"] == "ACTIVO":
        accumulated, percent, stage, status_class, badge_class = _tire_metrics(tire, vehicle["current_km"])

    history = query_all(
        """SELECT * FROM tires WHERE vehicle_id = ? AND position_code = ? AND id != ?
           ORDER BY install_date DESC, id DESC""",
        (tire["vehicle_id"], tire["position_code"], tire_id),
    )

    chain = _tire_journey(tire)
    journey = _journey_rows(chain, tire_id) if len(chain) > 1 else []

    inventory_tire = (
        query_one("SELECT * FROM tire_inventory WHERE id = ?", (tire["tire_inventory_id"],))
        if tire["tire_inventory_id"]
        else None
    )

    return render_template(
        "neumaticos/tire_detail.html",
        tire=tire,
        vehicle=vehicle,
        position_label=get_position_label(vehicle["vehicle_type"], tire["position_code"]),
        accumulated_km=accumulated,
        percent=percent,
        stage=stage,
        stage_label=LIFE_STAGE_LABELS.get(stage),
        badge_class=badge_class,
        history=history,
        journey=journey,
        inventory_tire=inventory_tire,
    )


@bp.route("/llanta/<int:tire_id>/reemplazar", methods=["GET", "POST"])
@permission_required("neumaticos", "edit")
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

        # La llanta nueva que va a esta posición debe existir y estar
        # DISPONIBLE en el inventario — se valida ANTES de retirar la
        # llanta vieja (_apply_disposition), para no dejar la posición
        # vacía si la selección de la nueva falla.
        new_tire_inventory_id = parse_float(request.form.get("new_tire_inventory_id"), None)
        new_inv_tire = (
            query_one(
                "SELECT * FROM tire_inventory WHERE id = ? AND status = 'DISPONIBLE'",
                (int(new_tire_inventory_id),),
            )
            if new_tire_inventory_id
            else None
        )
        if new_inv_tire is None:
            flash(
                "Selecciona la llanta del inventario que se instala en esta posición — "
                "primero debe estar registrada y disponible.",
                "error",
            )
            vehicles, vehicles_data, positions_by_type = _move_destination_data()
            return render_template(
                "neumaticos/tire_form.html",
                vehicle=vehicle,
                position_label=get_position_label(vehicle["vehicle_type"], old_tire["position_code"]),
                mode="replace",
                tire=old_tire,
                today=today_str(),
                default_expected_life_km=DEFAULT_EXPECTED_LIFE_KM,
                move_vehicles=vehicles,
                move_vehicles_data=vehicles_data,
                move_positions_by_type=positions_by_type,
                available_tires=_available_inventory_tires(),
                form=request.form,
            )

        if not _apply_disposition(old_tire, vehicle, removed_date, removed_km, removal_reason):
            vehicles, vehicles_data, positions_by_type = _move_destination_data()
            return render_template(
                "neumaticos/tire_form.html",
                vehicle=vehicle,
                position_label=get_position_label(vehicle["vehicle_type"], old_tire["position_code"]),
                mode="replace",
                tire=old_tire,
                today=today_str(),
                default_expected_life_km=DEFAULT_EXPECTED_LIFE_KM,
                move_vehicles=vehicles,
                move_vehicles_data=vehicles_data,
                move_positions_by_type=positions_by_type,
                available_tires=_available_inventory_tires(),
                form=request.form,
            )

        install_date = parse_date(request.form.get("new_install_date")) or today_str()
        km_at_install = parse_float(request.form.get("new_km_at_install"), removed_km)
        expected_life_km = parse_float(request.form.get("new_expected_life_km"), new_inv_tire["expected_life_km"])
        notes = request.form.get("new_notes", "").strip()
        db = get_db()
        db.execute(
            """INSERT INTO tires (vehicle_id, position_code, brand, install_date, km_at_install,
               expected_life_km, notes, tire_inventory_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                old_tire["vehicle_id"], old_tire["position_code"], new_inv_tire["brand"], install_date,
                km_at_install, expected_life_km, notes or None, new_inv_tire["id"],
            ),
        )
        db.execute("UPDATE tire_inventory SET status = 'ASIGNADA' WHERE id = ?", (new_inv_tire["id"],))
        db.commit()
        flash(f'Llanta "{new_inv_tire["code"]}" instalada en esta posición.', "success")
        return redirect(url_for("neumaticos.diagram", vehicle_id=old_tire["vehicle_id"]))

    vehicles, vehicles_data, positions_by_type = _move_destination_data()
    return render_template(
        "neumaticos/tire_form.html",
        vehicle=vehicle,
        position_label=get_position_label(vehicle["vehicle_type"], old_tire["position_code"]),
        mode="replace",
        tire=old_tire,
        today=today_str(),
        default_expected_life_km=DEFAULT_EXPECTED_LIFE_KM,
        move_vehicles=vehicles,
        move_vehicles_data=vehicles_data,
        move_positions_by_type=positions_by_type,
        available_tires=_available_inventory_tires(),
        form=None,
    )


@bp.route("/llanta/<int:tire_id>/retirar", methods=["GET", "POST"])
@permission_required("neumaticos", "edit")
def retire_tire(tire_id):
    tire = query_one("SELECT * FROM tires WHERE id = ?", (tire_id,))
    if tire is None:
        abort(404)
    if tire["status"] != "ACTIVO":
        flash("Esta llanta ya fue retirada.", "error")
        return redirect(url_for("neumaticos.detail", tire_id=tire_id))
    vehicle = _get_vehicle_or_404(tire["vehicle_id"])

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        removed_date = parse_date(request.form.get("removed_date")) or today_str()
        removed_km = parse_float(request.form.get("removed_km"), vehicle["current_km"] or 0)
        removal_reason = request.form.get("removal_reason", "").strip()

        if not _apply_disposition(tire, vehicle, removed_date, removed_km, removal_reason):
            vehicles, vehicles_data, positions_by_type = _move_destination_data()
            return render_template(
                "neumaticos/tire_form.html",
                vehicle=vehicle,
                position_label=get_position_label(vehicle["vehicle_type"], tire["position_code"]),
                mode="retire",
                tire=tire,
                today=today_str(),
                default_expected_life_km=DEFAULT_EXPECTED_LIFE_KM,
                move_vehicles=vehicles,
                move_vehicles_data=vehicles_data,
                move_positions_by_type=positions_by_type,
                form=request.form,
            )

        return redirect(url_for("neumaticos.diagram", vehicle_id=tire["vehicle_id"]))

    vehicles, vehicles_data, positions_by_type = _move_destination_data()
    return render_template(
        "neumaticos/tire_form.html",
        vehicle=vehicle,
        position_label=get_position_label(vehicle["vehicle_type"], tire["position_code"]),
        mode="retire",
        tire=tire,
        today=today_str(),
        default_expected_life_km=DEFAULT_EXPECTED_LIFE_KM,
        move_vehicles=vehicles,
        move_vehicles_data=vehicles_data,
        move_positions_by_type=positions_by_type,
        form=None,
    )


@bp.route("/unidad/<int:vehicle_id>/rotar", methods=["GET", "POST"])
@permission_required("neumaticos", "edit")
def rotate_tires(vehicle_id):
    """Rota llantas ACTIVAS entre posiciones de la misma unidad para parejar
    el desgaste. No toca km_at_install ni expected_life_km de cada llanta
    (su acumulado se sigue calculando igual, solo que ahora medido desde su
    nueva posición) — únicamente cambia position_code, y deja un registro
    en tire_rotations/tire_rotation_moves con el detalle de qué llanta pasó
    de dónde a dónde."""
    vehicle = _get_vehicle_or_404(vehicle_id)
    positions = get_positions(vehicle["vehicle_type"])
    position_labels = {p["code"]: p["label"] for p in positions}
    active_tires = query_all(
        "SELECT * FROM tires WHERE vehicle_id = ? AND status = 'ACTIVO' ORDER BY position_code",
        (vehicle_id,),
    )
    if len(active_tires) < 2:
        flash("Se necesitan al menos 2 llantas activas instaladas para poder rotar.", "error")
        return redirect(url_for("neumaticos.diagram", vehicle_id=vehicle_id))

    if request.method == "POST":
        if not validate_csrf():
            abort(400)

        # Posición de destino elegida para cada llanta activa (por defecto,
        # la misma que ya tiene si no se tocó su selector).
        chosen = {}
        for tire in active_tires:
            new_code = request.form.get(f"position_{tire['id']}", tire["position_code"])
            if new_code not in position_labels:
                flash("Posición de destino inválida.", "error")
                return redirect(url_for("neumaticos.rotate_tires", vehicle_id=vehicle_id))
            chosen[tire["id"]] = new_code

        # El resultado final no puede repetir ninguna posición entre las
        # llantas activas (se muevan o no).
        seen = {}
        for tire in active_tires:
            new_code = chosen[tire["id"]]
            if new_code in seen:
                flash(
                    f'No se puede dejar más de una llanta en la posición "{position_labels[new_code]}" '
                    "— revisa que las posiciones de destino no se repitan.",
                    "error",
                )
                return redirect(url_for("neumaticos.rotate_tires", vehicle_id=vehicle_id))
            seen[new_code] = tire["id"]

        moves = [
            (tire, tire["position_code"], chosen[tire["id"]])
            for tire in active_tires
            if chosen[tire["id"]] != tire["position_code"]
        ]
        if not moves:
            flash("No seleccionaste ningún cambio de posición.", "error")
            return redirect(url_for("neumaticos.rotate_tires", vehicle_id=vehicle_id))

        rotation_date = parse_date(request.form.get("rotation_date")) or today_str()
        km_at_rotation = (
            parse_float(request.form.get("km_at_rotation"), vehicle["current_km"])
            if request.form.get("km_at_rotation", "").strip()
            else vehicle["current_km"]
        )
        notes = request.form.get("notes", "").strip()

        db = get_db()
        # Dos pasadas: primero a un código temporal único por llanta, luego
        # al destino final — evita chocar con el índice único de posición
        # activa cuando dos llantas se intercambian entre sí.
        for tire, _from_code, _to_code in moves:
            db.execute(
                "UPDATE tires SET position_code = ? WHERE id = ?",
                (f"__ROT_TMP_{tire['id']}", tire["id"]),
            )
        for tire, _from_code, to_code in moves:
            db.execute("UPDATE tires SET position_code = ? WHERE id = ?", (to_code, tire["id"]))
        db.commit()

        rotation_id = execute(
            """INSERT INTO tire_rotations (vehicle_id, rotation_date, km_at_rotation, notes)
               VALUES (?, ?, ?, ?)""",
            (vehicle_id, rotation_date, km_at_rotation, notes or None),
        )
        db = get_db()
        for tire, from_code, to_code in moves:
            db.execute(
                """INSERT INTO tire_rotation_moves (rotation_id, tire_id, from_position_code, to_position_code)
                   VALUES (?, ?, ?, ?)""",
                (rotation_id, tire["id"], from_code, to_code),
            )
        db.commit()

        flash(f"Rotación registrada: {len(moves)} llanta(s) cambiaron de posición.", "success")
        return redirect(url_for("neumaticos.diagram", vehicle_id=vehicle_id))

    tires_for_form = [
        {"tire": t, "position_label": position_labels.get(t["position_code"], t["position_code"])}
        for t in active_tires
    ]
    return render_template(
        "neumaticos/rotate_form.html",
        vehicle=vehicle,
        type_label=VEHICLE_TYPE_LABELS.get(vehicle["vehicle_type"], vehicle["vehicle_type"]),
        tires_for_form=tires_for_form,
        positions=positions,
        today=today_str(),
    )
