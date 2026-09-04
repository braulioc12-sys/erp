import functools
import os
import uuid

from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

from app import storage
from app.auth import can, login_required, permission_required, validate_csrf
from app.db import execute, query_all, query_one
from app.helpers import compress_photo, next_code, now_str, parse_date, parse_float, today_str
from app.routes.rutas import find_route

bp = Blueprint("viajes", __name__, url_prefix="/viajes")

STATUS_FLOW = {
    "PENDIENTE": ["EN_CURSO", "CANCELADO"],
    "EN_CURSO": ["ENTREGADO", "CANCELADO"],
    "ENTREGADO": [],
    "CANCELADO": [],
}

# 3 sep, pedido de Braulio ("cambios en el módulo de viajes") — mismos
# valores/patrón que quotations.issuer (ver app/routes/cotizaciones.py):
# empresa que opera el viaje.
ISSUER_CHOICES = ("HARRASO", "BRMS")

CARGO_TYPES = [
    ("PLATAFORMA", "Plataforma"),
    ("CONTENEDOR", "Contenedor"),
    ("PARIHUELERO", "Parihuelero"),
    ("FURGON", "Furgón"),
    ("OTROS", "Otros"),
]

# "Periodo de pago" de un viaje con terceros: lista cerrada de términos
# comunes (pedido explícito de Braulio, en vez de texto libre) para poder
# agrupar/filtrar de forma consistente más adelante.
PAYMENT_TERMS = [
    ("CONTADO", "Contado"),
    ("15_DIAS", "15 días"),
    ("30_DIAS", "30 días"),
    ("45_DIAS", "45 días"),
    ("60_DIAS", "60 días"),
]

# Archivos adjuntos a un viaje — guía de transportista (3 sep) y, desde el
# 4 sep, conformidad de entrega: mismo criterio de formatos permitidos que
# los comprobantes de Liquidaciones (ver ALLOWED_RECEIPT_EXTENSIONS en
# app/routes/liquidaciones.py): foto o PDF.
ALLOWED_ATTACHMENT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".pdf", ".webp", ".heic", ".heif"}
ATTACHMENT_MIME_TO_EXTENSION = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "application/pdf": ".pdf",
}


def _parse_issuer(form):
    issuer = (form.get("issuer") or "").strip().upper()
    return issuer if issuer in ISSUER_CHOICES else "HARRASO"


def _parse_cargo_type(form):
    value = (form.get("cargo_type") or "").strip().upper()
    valid = {code for code, _ in CARGO_TYPES}
    return value if value in valid else None


def _parse_ownership(form):
    value = (form.get("ownership") or "").strip().upper()
    return "TERCERO" if value == "TERCERO" else "PROPIA"


def _parse_payment_term(form):
    value = (form.get("third_party_payment_term") or "").strip().upper()
    valid = {code for code, _ in PAYMENT_TERMS}
    return value if value in valid else None


def _billing_permission_required(view):
    """Permiso para marcar Facturado/Pagado (3 sep): además de quien ya
    puede editar Viajes (Admin/Despachador/Operador), también Contabilidad
    — que no tiene "editar" en Viajes (solo "ver", ya que su trabajo normal
    es Liquidaciones/Facturación) pero sí necesita poder marcar estos dos
    estados de cobranza. Por eso este es un chequeo aparte en vez de
    @permission_required("viajes", "edit") directo."""

    @functools.wraps(view)
    @login_required
    def wrapped_view(**kwargs):
        if not (can(g.user["roles"], "viajes", "edit") or can(g.user["roles"], "liquidaciones", "edit")):
            flash("No tienes permiso para acceder a esta sección.", "error")
            return redirect(url_for("dashboard.index"))
        return view(**kwargs)

    return wrapped_view


@bp.route("")
@permission_required("viajes", "view")
def list_view():
    """3 sep, pedido de Braulio: por defecto el panel general de Viajes solo
    muestra los de unidad propia — los de terceros tienen su propio listado
    (ver list_terceros), con otras columnas relevantes para ese caso."""
    status = request.args.get("status", "")
    q = request.args.get("q", "").strip()

    sql = """SELECT t.*, c.name as client_name, v.plate as vehicle_plate, tv.plate as trailer_plate,
                     d.name as driver_name, d2.name as driver2_name
              FROM trips t
              JOIN clients c ON c.id = t.client_id
              LEFT JOIN vehicles v ON v.id = t.vehicle_id
              LEFT JOIN vehicles tv ON tv.id = t.trailer_vehicle_id
              LEFT JOIN drivers d ON d.id = t.driver_id
              LEFT JOIN drivers d2 ON d2.id = t.driver2_id
              WHERE t.ownership = 'PROPIA'"""
    params = []
    if status:
        sql += " AND t.status = ?"
        params.append(status)
    if q:
        sql += " AND (t.code LIKE ? OR c.name LIKE ? OR t.origin LIKE ? OR t.destination LIKE ?)"
        params += [f"%{q}%"] * 4
    sql += " ORDER BY t.scheduled_date DESC, t.id DESC"

    trips = query_all(sql, params)
    terceros_count = query_one("SELECT COUNT(*) n FROM trips WHERE ownership = 'TERCERO'")["n"]
    return render_template("viajes/list.html", trips=trips, status=status, q=q, terceros_count=terceros_count)


@bp.route("/terceros")
@permission_required("viajes", "view")
def list_terceros():
    """3 sep, pedido de Braulio: listado aparte para viajes subcontratados a
    terceros, con las columnas que pidió — fecha de viaje, estado, periodo
    de pago y cancelado (sí/no) — además de lo mínimo para identificar cada
    viaje (código, cliente, tercero)."""
    status = request.args.get("status", "")
    q = request.args.get("q", "").strip()

    sql = """SELECT t.*, c.name as client_name
              FROM trips t
              JOIN clients c ON c.id = t.client_id
              WHERE t.ownership = 'TERCERO'"""
    params = []
    if status:
        sql += " AND t.status = ?"
        params.append(status)
    if q:
        sql += """ AND (t.code LIKE ? OR c.name LIKE ? OR t.third_party_name LIKE ?
                         OR t.origin LIKE ? OR t.destination LIKE ?)"""
        params += [f"%{q}%"] * 5
    sql += " ORDER BY t.scheduled_date DESC, t.id DESC"

    trips = query_all(sql, params)
    payment_term_labels = dict(PAYMENT_TERMS)
    return render_template(
        "viajes/list_terceros.html", trips=trips, status=status, q=q, payment_term_labels=payment_term_labels
    )


def _active_routes():
    """Rutas activas del catálogo, para el desplegable de selección del
    formulario de viajes (ya no se escribe origen/destino a mano — pedido
    de Braulio, 28 ago: la ruta se elige de las que ya están registradas
    en Rutas). Se convierte a dicts porque sqlite3.Row no es serializable
    a JSON directamente (se usa también para la sugerencia de comisión en JS)."""
    rows = query_all(
        "SELECT id, origin, destination, default_commission_amount FROM routes WHERE active = 1 ORDER BY origin, destination"
    )
    return [dict(r) for r in rows]


def _active_vehicles(current_vehicle_id=None):
    """Unidades tracto/camión disponibles para el campo "Unidad" del
    formulario de viajes — excluye las de tipo CARRETA (3 sep: la carreta
    ahora se elige aparte, ver _active_trailers). Incluye la unidad ya
    asignada al viaje aunque esté inactiva/sea CARRETA, para no perderla del
    desplegable al editar un viaje viejo (mismo criterio que ya se usaba)."""
    return query_all(
        "SELECT * FROM vehicles WHERE (vehicle_type != 'CARRETA' AND status = 'ACTIVO') OR id = ? ORDER BY plate",
        (current_vehicle_id,),
    )


def _active_trailers(current_trailer_id=None):
    """Carretas (semirremolques) disponibles para el campo "Carreta" del
    formulario de viajes (3 sep, pedido de Braulio: "se debe seleccionar
    tanto la unidad tracto como la carreta")."""
    return query_all(
        "SELECT * FROM vehicles WHERE (vehicle_type = 'CARRETA' AND status = 'ACTIVO') OR id = ? ORDER BY plate",
        (current_trailer_id,),
    )


def _resolve_route_selection(form, current_trip=None):
    """Resuelve la ruta elegida en el desplegable del formulario de viajes.
    Devuelve (origin, destination, route_row_o_None, error_o_None).

    `route_id` normalmente es el id de una ruta activa del catálogo. El
    valor especial "__current__" solo aparece al editar un viaje cuya
    ruta (origen/destino ya guardados) no está en el catálogo activo —
    deja la ruta tal cual estaba en vez de obligar a elegir una nueva
    sin querer."""
    route_id = (form.get("route_id") or "").strip()
    if route_id == "__current__" and current_trip is not None:
        return current_trip["origin"], current_trip["destination"], None, None
    if not route_id:
        return "", "", None, "Selecciona una ruta del catálogo (Rutas)."
    route = query_one("SELECT * FROM routes WHERE id = ? AND active = 1", (route_id,))
    if not route:
        return "", "", None, "La ruta elegida ya no está disponible — elige otra."
    return route["origin"], route["destination"], route, None


def _resolve_commission(form, origin, destination, route=None, double_driver=False, single_leg=False):
    """Si el usuario dejó vacío el campo de comisión, se usa el monto
    predeterminado de la ruta elegida (o, si no se resolvió una ruta
    directamente — caso "__current__" —, se busca por origen/destino),
    ajustado según "doble conductor" (x0.6) y "solo 1 tramo" (x0.5) —
    pedido de Braulio, 3 sep. Si ambos están marcados se combinan
    multiplicando (60% x 50% = 30%). Cuando hay doble conductor, este
    mismo monto (completo, sin repartir) se le asigna a cada uno de los
    2 conductores — ver INSERT/UPDATE en new()/edit()."""
    raw = (form.get("driver_commission") or "").strip()
    if raw:
        return parse_float(raw, 0)
    if route is None:
        route = find_route(origin, destination)
    base = route["default_commission_amount"] if route else 0
    factor = 1.0
    if double_driver:
        factor *= 0.6
    if single_leg:
        factor *= 0.5
    return base * factor


def _selected_route_id_for_edit(trip):
    """Id a preseleccionar en el desplegable al editar un viaje: el de la
    ruta activa que coincide con el origen/destino ya guardados, o
    "__current__" si esa ruta ya no está en el catálogo activo."""
    route = find_route(trip["origin"], trip["destination"])
    return str(route["id"]) if route else "__current__"


def _ownership_and_third_party_fields(form):
    """Resuelve, a partir del formulario, los campos de unidad propia vs.
    tercero (3 sep, pedido de Braulio). Devuelve un dict listo para pasar
    al INSERT/UPDATE, y una lista de errores de validación. Si es unidad
    propia, los campos de tercero quedan en None (y viceversa) — nunca se
    guardan los dos juntos."""
    ownership = _parse_ownership(form)
    errors = []
    fields = {
        "ownership": ownership,
        "vehicle_id": None,
        "trailer_vehicle_id": None,
        "third_party_name": None,
        "third_party_unit": None,
        "third_party_rate": None,
        "third_party_payment_term": None,
    }
    if ownership == "PROPIA":
        fields["vehicle_id"] = form.get("vehicle_id") or None
        fields["trailer_vehicle_id"] = form.get("trailer_vehicle_id") or None
        if not fields["vehicle_id"]:
            errors.append("Selecciona la unidad tracto.")
        if not fields["trailer_vehicle_id"]:
            errors.append("Selecciona la carreta.")
    else:
        fields["third_party_name"] = (form.get("third_party_name") or "").strip() or None
        fields["third_party_unit"] = (form.get("third_party_unit") or "").strip() or None
        fields["third_party_rate"] = parse_float(form.get("third_party_rate"), None)
        fields["third_party_payment_term"] = _parse_payment_term(form)
        if not fields["third_party_name"]:
            errors.append("Ingresa el nombre de la empresa/tercero que hace el viaje.")
        if fields["third_party_rate"] is None:
            errors.append("Ingresa el flete acordado con el tercero.")
        if not fields["third_party_payment_term"]:
            errors.append("Selecciona el periodo de pago del tercero.")
    return fields, errors


@bp.route("/nuevo", methods=["GET", "POST"])
@permission_required("viajes", "edit")
def new():
    # 4 sep, pedido de Braulio: "la primera pantalla" al crear un viaje es
    # elegir Harraso/BRMS y unidad propia (default) o tercera — recién con
    # eso elegido se muestra el resto del formulario. `step=2` en la query
    # string es la señal de que el paso 1 ya se completó (viene del propio
    # <form method="get"> de new_step1.html); sin eso, siempre se muestra el
    # paso 1, incluso en un POST fallido no debería ocurrir porque el POST
    # real llega desde el formulario del paso 2, que ya lo incluye como
    # campos ocultos, no como este parámetro de navegación.
    if request.method == "GET" and request.args.get("step") != "2":
        return render_template("viajes/new_step1.html")

    clients = query_all("SELECT * FROM clients WHERE active = 1 ORDER BY name")
    vehicles = _active_vehicles()
    trailers = _active_trailers()
    drivers = query_all("SELECT * FROM drivers WHERE status = 'ACTIVO' ORDER BY name")
    routes = _active_routes()

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        client_id = request.form.get("client_id")
        origin, destination, route, route_error = _resolve_route_selection(request.form)
        scheduled_date = parse_date(request.form.get("scheduled_date"))
        double_driver = bool(request.form.get("double_driver"))
        single_leg = bool(request.form.get("single_leg"))
        driver_id = request.form.get("driver_id") or None
        driver2_id = (request.form.get("driver2_id") or None) if double_driver else None
        issuer = _parse_issuer(request.form)
        cargo_type = _parse_cargo_type(request.form)
        ownership_fields, ownership_errors = _ownership_and_third_party_fields(request.form)
        errors = list(ownership_errors)
        if not client_id:
            errors.append("Selecciona un cliente.")
        if route_error:
            errors.append(route_error)
        if not scheduled_date:
            errors.append("La fecha programada no es válida.")
        if not cargo_type:
            errors.append("Selecciona el tipo de carga.")
        if double_driver:
            if not driver2_id:
                errors.append("Selecciona el segundo conductor (viaje de doble conductor).")
            elif driver_id and driver2_id == driver_id:
                errors.append("El segundo conductor debe ser distinto del primero.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "viajes/form.html", trip=request.form, mode="new",
                clients=clients, vehicles=vehicles, trailers=trailers, drivers=drivers, routes=routes,
                selected_route_id=request.form.get("route_id", ""),
                cargo_types=CARGO_TYPES, payment_terms=PAYMENT_TERMS,
            )

        code = next_code("V", "trips")
        driver_commission = _resolve_commission(
            request.form, origin, destination, route,
            double_driver=double_driver, single_leg=single_leg,
        )
        trip_id = execute(
            """INSERT INTO trips (code, client_id, vehicle_id, trailer_vehicle_id, driver_id, driver2_id,
               origin, destination, cargo_description, cargo_weight_kg, cargo_type, scheduled_date, rate,
               driver_commission, double_driver, single_leg, notes, issuer, ownership,
               third_party_name, third_party_unit, third_party_rate, third_party_payment_term, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                code,
                client_id,
                ownership_fields["vehicle_id"],
                ownership_fields["trailer_vehicle_id"],
                driver_id,
                driver2_id,
                origin,
                destination,
                request.form.get("cargo_description", "").strip(),
                parse_float(request.form.get("cargo_weight_kg"), None),
                cargo_type,
                scheduled_date,
                parse_float(request.form.get("rate")),
                driver_commission,
                int(double_driver),
                int(single_leg),
                request.form.get("notes", "").strip(),
                issuer,
                ownership_fields["ownership"],
                ownership_fields["third_party_name"],
                ownership_fields["third_party_unit"],
                ownership_fields["third_party_rate"],
                ownership_fields["third_party_payment_term"],
                None,
            ),
        )
        flash(f"Viaje {code} creado.", "success")
        return redirect(url_for("viajes.detail", trip_id=trip_id))

    # Llega desde el paso 1 (empresa/unidad ya elegidos en la query string) —
    # se preseleccionan en el paso 2 y quedan fijos para esta creación (ver
    # viajes/form.html, bloque `mode == 'new'`).
    return render_template(
        "viajes/form.html", trip=None, mode="new",
        clients=clients, vehicles=vehicles, trailers=trailers, drivers=drivers, routes=routes, today=today_str(),
        selected_route_id="", cargo_types=CARGO_TYPES, payment_terms=PAYMENT_TERMS,
        preset_issuer=_parse_issuer(request.args), preset_ownership=_parse_ownership(request.args),
    )


@bp.route("/<int:trip_id>/editar", methods=["GET", "POST"])
@permission_required("viajes", "edit")
def edit(trip_id):
    trip = query_one("SELECT * FROM trips WHERE id = ?", (trip_id,))
    if trip is None:
        abort(404)
    clients = query_all("SELECT * FROM clients WHERE active = 1 ORDER BY name")
    vehicles = _active_vehicles(trip["vehicle_id"])
    trailers = _active_trailers(trip["trailer_vehicle_id"])
    drivers = query_all(
        "SELECT * FROM drivers WHERE status = 'ACTIVO' OR id = ? OR id = ? ORDER BY name",
        (trip["driver_id"], trip["driver2_id"]),
    )
    routes = _active_routes()

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        scheduled_date = parse_date(request.form.get("scheduled_date")) or trip["scheduled_date"]
        origin, destination, route, route_error = _resolve_route_selection(request.form, current_trip=trip)
        double_driver = bool(request.form.get("double_driver"))
        single_leg = bool(request.form.get("single_leg"))
        driver_id = request.form.get("driver_id") or None
        driver2_id = (request.form.get("driver2_id") or None) if double_driver else None
        issuer = _parse_issuer(request.form)
        cargo_type = _parse_cargo_type(request.form)
        ownership_fields, ownership_errors = _ownership_and_third_party_fields(request.form)
        errors = list(ownership_errors)
        if route_error:
            errors.append(route_error)
        if not cargo_type:
            errors.append("Selecciona el tipo de carga.")
        if double_driver:
            if not driver2_id:
                errors.append("Selecciona el segundo conductor (viaje de doble conductor).")
            elif driver_id and driver2_id == driver_id:
                errors.append("El segundo conductor debe ser distinto del primero.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "viajes/form.html", trip=trip, mode="edit", trip_id=trip_id,
                clients=clients, vehicles=vehicles, trailers=trailers, drivers=drivers, routes=routes,
                selected_route_id=request.form.get("route_id", ""),
                cargo_types=CARGO_TYPES, payment_terms=PAYMENT_TERMS,
            )
        driver_commission = _resolve_commission(
            request.form, origin, destination, route,
            double_driver=double_driver, single_leg=single_leg,
        )
        execute(
            """UPDATE trips SET client_id=?, vehicle_id=?, trailer_vehicle_id=?, driver_id=?, driver2_id=?,
               origin=?, destination=?, cargo_description=?, cargo_weight_kg=?, cargo_type=?, scheduled_date=?,
               rate=?, driver_commission=?, double_driver=?, single_leg=?, notes=?, issuer=?, ownership=?,
               third_party_name=?, third_party_unit=?, third_party_rate=?, third_party_payment_term=?
               WHERE id=?""",
            (
                request.form.get("client_id"),
                ownership_fields["vehicle_id"],
                ownership_fields["trailer_vehicle_id"],
                driver_id,
                driver2_id,
                origin,
                destination,
                request.form.get("cargo_description", "").strip(),
                parse_float(request.form.get("cargo_weight_kg"), None),
                cargo_type,
                scheduled_date,
                parse_float(request.form.get("rate")),
                driver_commission,
                int(double_driver),
                int(single_leg),
                request.form.get("notes", "").strip(),
                issuer,
                ownership_fields["ownership"],
                ownership_fields["third_party_name"],
                ownership_fields["third_party_unit"],
                ownership_fields["third_party_rate"],
                ownership_fields["third_party_payment_term"],
                trip_id,
            ),
        )
        flash("Viaje actualizado.", "success")
        return redirect(url_for("viajes.detail", trip_id=trip_id))

    return render_template(
        "viajes/form.html", trip=trip, mode="edit", trip_id=trip_id,
        clients=clients, vehicles=vehicles, trailers=trailers, drivers=drivers, routes=routes,
        selected_route_id=_selected_route_id_for_edit(trip),
        cargo_types=CARGO_TYPES, payment_terms=PAYMENT_TERMS,
    )


@bp.route("/<int:trip_id>")
@permission_required("viajes", "view")
def detail(trip_id):
    trip = query_one(
        """SELECT t.*, c.name as client_name, v.plate as vehicle_plate, tv.plate as trailer_plate,
                  d.name as driver_name, d2.name as driver2_name
           FROM trips t
           JOIN clients c ON c.id = t.client_id
           LEFT JOIN vehicles v ON v.id = t.vehicle_id
           LEFT JOIN vehicles tv ON tv.id = t.trailer_vehicle_id
           LEFT JOIN drivers d ON d.id = t.driver_id
           LEFT JOIN drivers d2 ON d2.id = t.driver2_id
           WHERE t.id = ?""",
        (trip_id,),
    )
    if trip is None:
        abort(404)
    expenses = query_all("SELECT * FROM expenses WHERE trip_id = ? ORDER BY expense_date DESC", (trip_id,))
    total_expenses = sum(e["amount"] for e in expenses)
    next_statuses = STATUS_FLOW.get(trip["status"], [])
    advance = query_one("SELECT id, status FROM expense_advances WHERE trip_id = ?", (trip_id,))
    payment_term_labels = dict(PAYMENT_TERMS)
    cargo_type_labels = dict(CARGO_TYPES)
    return render_template(
        "viajes/detail.html", trip=trip, expenses=expenses,
        total_expenses=total_expenses, next_statuses=next_statuses, advance=advance,
        payment_term_labels=payment_term_labels, cargo_type_labels=cargo_type_labels,
    )


@bp.route("/<int:trip_id>/estado", methods=["POST"])
@permission_required("viajes", "edit")
def change_status(trip_id):
    if not validate_csrf():
        abort(400)
    trip = query_one("SELECT * FROM trips WHERE id = ?", (trip_id,))
    if trip is None:
        abort(404)
    new_status = request.form.get("status")
    allowed = STATUS_FLOW.get(trip["status"], [])
    if new_status not in allowed:
        flash("Cambio de estado no permitido.", "error")
        return redirect(url_for("viajes.detail", trip_id=trip_id))

    if new_status == "ENTREGADO":
        # 4 sep, pedido de Braulio: adjuntar la conformidad de entrega es lo
        # que habilita marcar el viaje como Entregado — este chequeo evita
        # llegar a ENTREGADO sin ese archivo, sin importar por dónde se
        # mande el POST (el flujo normal es save_delivery_proof(), que
        # adjunta el archivo y hace esta misma transición en un solo paso).
        if not trip["delivery_proof_filename"]:
            flash("Antes de marcar el viaje como Entregado, adjunta la conformidad de entrega.", "error")
            return redirect(url_for("viajes.detail", trip_id=trip_id))
        # 31 ago: además de la fecha (ya existía), se guarda el momento
        # exacto de inicio/fin real del viaje — es lo que necesita el futuro
        # reporte de cumplimiento de hoja de ruta para saber qué tramo del
        # historial de GPS corresponde a este viaje.
        execute(
            "UPDATE trips SET status=?, delivered_date=?, actual_end_at=? WHERE id=?",
            (new_status, today_str(), now_str(), trip_id),
        )
    elif new_status == "EN_CURSO":
        execute(
            "UPDATE trips SET status=?, actual_start_at=COALESCE(actual_start_at, ?) WHERE id=?",
            (new_status, now_str(), trip_id),
        )
    else:
        execute("UPDATE trips SET status=? WHERE id=?", (new_status, trip_id))

    flash(f"Viaje marcado como {new_status.replace('_', ' ').title()}.", "success")
    return redirect(url_for("viajes.detail", trip_id=trip_id))


# --- Guía de transportista (3 sep, pedido de Braulio) ---------------------
#
# Documento propio del transportista/tercero que hizo el viaje — distinto
# de la guía de remisión SUNAT que ya genera el módulo Guías (ver
# viajes/detail.html, botón "Generar guía de remisión"). Se agrega DESPUÉS
# de creado el viaje (no en el formulario de alta), con un número a mano
# y/o un archivo (foto o PDF) — mismo mecanismo de almacenamiento que los
# comprobantes de Liquidaciones y las fotos de Conductores (ver
# app/storage.py).

def _save_binary_attachment(file_storage, save_fn):
    """Guarda un archivo adjunto (foto o PDF) usando `save_fn(filename,
    raw_bytes)` y devuelve el nombre guardado, o None si no se subió nada
    válido. Mismo patrón para la guía de transportista y la conformidad de
    entrega: detecta la extensión real (por nombre o por mimetype), las
    fotos se recomprimen (mismo criterio que comprobantes/fotos de
    conductores), los PDF se guardan tal cual."""
    if not file_storage or not file_storage.filename:
        return None
    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in ALLOWED_ATTACHMENT_EXTENSIONS:
        ext = ATTACHMENT_MIME_TO_EXTENSION.get((file_storage.mimetype or "").lower())
    if not ext:
        return None
    raw_bytes = file_storage.read()
    if not raw_bytes:
        return None
    if ext == ".pdf":
        filename = f"{uuid.uuid4().hex}.pdf"
        save_fn(filename, raw_bytes)
        return filename
    compressed = compress_photo(raw_bytes)
    if compressed is not None:
        filename = f"{uuid.uuid4().hex}.jpg"
        save_fn(filename, compressed)
        return filename
    filename = f"{uuid.uuid4().hex}{ext}"
    save_fn(filename, raw_bytes)
    return filename


def _save_waybill_file(file_storage):
    return _save_binary_attachment(file_storage, storage.save_carrier_waybill)


def _save_delivery_proof_file(file_storage):
    return _save_binary_attachment(file_storage, storage.save_delivery_proof)


@bp.route("/<int:trip_id>/guia", methods=["POST"])
@permission_required("viajes", "edit")
def save_waybill(trip_id):
    if not validate_csrf():
        abort(400)
    trip = query_one("SELECT carrier_waybill_filename FROM trips WHERE id = ?", (trip_id,))
    if trip is None:
        abort(404)
    number = request.form.get("carrier_waybill_number", "").strip() or None
    new_filename = _save_waybill_file(request.files.get("carrier_waybill_file"))
    filename = new_filename if new_filename else trip["carrier_waybill_filename"]
    execute(
        "UPDATE trips SET carrier_waybill_number=?, carrier_waybill_filename=? WHERE id=?",
        (number, filename, trip_id),
    )
    flash("Guía de transportista guardada.", "success")
    return redirect(url_for("viajes.detail", trip_id=trip_id))


@bp.route("/<int:trip_id>/guia/archivo")
@permission_required("viajes", "view")
def waybill_file(trip_id):
    trip = query_one("SELECT carrier_waybill_filename FROM trips WHERE id = ?", (trip_id,))
    if trip is None or not trip["carrier_waybill_filename"]:
        abort(404)
    if storage.using_s3():
        return redirect(storage.carrier_waybill_url(trip["carrier_waybill_filename"]))
    return send_from_directory(storage.local_carrier_waybills_dir(), trip["carrier_waybill_filename"])


# --- Conformidad de entrega (4 sep, pedido de Braulio) ---------------------
#
# "cuando el viaje esté en curso, haya la opción de adjuntar conformidad de
# entrega para poder marcarlo como entregado" — a diferencia de la guía de
# transportista (que solo se guarda), adjuntar este archivo y marcar el
# viaje como ENTREGADO es UNA sola acción: no existe otra forma de llegar a
# ENTREGADO (ver el chequeo agregado en change_status()).

@bp.route("/<int:trip_id>/conformidad", methods=["POST"])
@permission_required("viajes", "edit")
def save_delivery_proof(trip_id):
    if not validate_csrf():
        abort(400)
    trip = query_one("SELECT status, delivery_proof_filename FROM trips WHERE id = ?", (trip_id,))
    if trip is None:
        abort(404)
    if trip["status"] != "EN_CURSO":
        flash("Solo se puede adjuntar la conformidad de entrega mientras el viaje está en curso.", "error")
        return redirect(url_for("viajes.detail", trip_id=trip_id))
    new_filename = _save_delivery_proof_file(request.files.get("delivery_proof_file"))
    filename = new_filename if new_filename else trip["delivery_proof_filename"]
    if not filename:
        flash("Adjunta una foto o PDF de la conformidad de entrega.", "error")
        return redirect(url_for("viajes.detail", trip_id=trip_id))
    execute(
        "UPDATE trips SET delivery_proof_filename=?, status=?, delivered_date=?, actual_end_at=? WHERE id=?",
        (filename, "ENTREGADO", today_str(), now_str(), trip_id),
    )
    flash("Conformidad de entrega adjuntada — viaje marcado como Entregado.", "success")
    return redirect(url_for("viajes.detail", trip_id=trip_id))


@bp.route("/<int:trip_id>/conformidad/archivo")
@permission_required("viajes", "view")
def delivery_proof_file(trip_id):
    trip = query_one("SELECT delivery_proof_filename FROM trips WHERE id = ?", (trip_id,))
    if trip is None or not trip["delivery_proof_filename"]:
        abort(404)
    if storage.using_s3():
        return redirect(storage.delivery_proof_url(trip["delivery_proof_filename"]))
    return send_from_directory(storage.local_delivery_proofs_dir(), trip["delivery_proof_filename"])


# --- Facturado / Pagado (3 sep, pedido de Braulio) -------------------------
#
# "invoiced" ya existía (se marca solo al generar una factura desde
# Facturación); ahora también se puede marcar/desmarcar a mano desde el
# viaje mismo. "paid" es un campo nuevo, independiente de "invoiced" — un
# viaje puede estar facturado pero no pagado todavía.

@bp.route("/<int:trip_id>/facturado", methods=["POST"])
@_billing_permission_required
def toggle_invoiced(trip_id):
    if not validate_csrf():
        abort(400)
    trip = query_one("SELECT invoiced FROM trips WHERE id = ?", (trip_id,))
    if trip is None:
        abort(404)
    new_value = 0 if trip["invoiced"] else 1
    execute("UPDATE trips SET invoiced=? WHERE id=?", (new_value, trip_id))
    flash("Viaje marcado como facturado." if new_value else "Viaje desmarcado como facturado.", "success")
    return redirect(url_for("viajes.detail", trip_id=trip_id))


@bp.route("/<int:trip_id>/pagado", methods=["POST"])
@_billing_permission_required
def toggle_paid(trip_id):
    if not validate_csrf():
        abort(400)
    trip = query_one("SELECT paid FROM trips WHERE id = ?", (trip_id,))
    if trip is None:
        abort(404)
    new_value = 0 if trip["paid"] else 1
    execute("UPDATE trips SET paid=? WHERE id=?", (new_value, trip_id))
    flash("Viaje marcado como pagado." if new_value else "Viaje desmarcado como pagado.", "success")
    return redirect(url_for("viajes.detail", trip_id=trip_id))


def _commissions_by_driver(month):
    """Agrupa, para un mes (YYYY-MM), cuántos viajes hizo cada conductor a
    cada ruta y cuál fue su comisión total. Excluye viajes cancelados.

    En un viaje de "doble conductor" (double_driver=1), driver_commission ya
    trae el monto completo que le corresponde a CADA conductor (pedido de
    Braulio, 3 sep: "cada conductor recibe el 60% completo", no se reparte) —
    por eso el segundo conductor se agrega con un UNION ALL que suma ese
    mismo monto otra vez, no la mitad."""
    rows = query_all(
        """SELECT driver_id, driver_name, origin, destination,
                  COUNT(*) as trip_count, SUM(driver_commission) as route_commission
           FROM (
               SELECT d.id as driver_id, d.name as driver_name, t.origin, t.destination,
                      t.driver_commission as driver_commission
               FROM trips t
               JOIN drivers d ON d.id = t.driver_id
               WHERE strftime('%Y-%m', t.scheduled_date) = ? AND t.status != 'CANCELADO'
               UNION ALL
               SELECT d2.id as driver_id, d2.name as driver_name, t.origin, t.destination,
                      t.driver_commission as driver_commission
               FROM trips t
               JOIN drivers d2 ON d2.id = t.driver2_id
               WHERE t.double_driver = 1 AND strftime('%Y-%m', t.scheduled_date) = ? AND t.status != 'CANCELADO'
           ) combined
           GROUP BY driver_id, origin, destination
           ORDER BY driver_name, origin, destination""",
        (month, month),
    )
    by_driver = {}
    for r in rows:
        entry = by_driver.setdefault(
            r["driver_id"],
            {"driver_name": r["driver_name"], "routes": [], "trip_count": 0, "total_commission": 0.0},
        )
        entry["routes"].append(r)
        entry["trip_count"] += r["trip_count"]
        entry["total_commission"] += r["route_commission"] or 0.0
    return list(by_driver.values())


@bp.route("/comisiones")
@permission_required("viajes", "view")
def commissions_report():
    month = request.args.get("month") or today_str()[:7]
    drivers = _commissions_by_driver(month)
    grand_total = sum(d["total_commission"] for d in drivers)
    grand_trips = sum(d["trip_count"] for d in drivers)
    return render_template(
        "viajes/commissions.html", month=month, drivers=drivers,
        grand_total=grand_total, grand_trips=grand_trips,
    )


@bp.route("/comisiones/exportar")
@permission_required("viajes", "view")
def commissions_export():
    from flask import current_app

    from app.reports import build_commissions_workbook

    month = request.args.get("month") or today_str()[:7]
    drivers = _commissions_by_driver(month)
    buffer = build_commissions_workbook(drivers, company_name=current_app.config["COMPANY_NAME"], month=month)
    filename = f"comisiones_{month}.xlsx"
    return Response(
        buffer.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
