import functools
import os
import re
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
from app.audit import get_creator_info, log_activity
from app.auth import can, login_required, permission_required, validate_csrf
from app.db import execute, query_all, query_one
from app.helpers import compress_photo, now_str, parse_date, parse_float, today_str
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

# 30 sep, pedido de Braulio: primero pidió agregar "Isotanque" a esta lista
# fija; antes de aplicar ese parche, pidió ir más allá ("mejor agregas en
# catalogos el tipo de carga para poder editar o agregar otros sin tener
# que subir un nuevo parche") -- dejó de ser una lista fija en código, ver
# _cargo_types() más abajo, que lee el catálogo editable en Catálogos →
# Tipos de carga (mismo mecanismo que _vehicle_owners() en
# app/routes/flota.py).

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

# Foto de evidencia del estado del contenedor (10 sep, pedido de Braulio) —
# solo aplica cuando cargo_type='CONTENEDOR'. A diferencia de los adjuntos
# de arriba, es siempre una foto (nunca un PDF), así que usa su propio
# conjunto de extensiones permitidas en vez de ALLOWED_ATTACHMENT_EXTENSIONS.
ALLOWED_CONTAINER_PHOTO_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif"}
CONTAINER_PHOTO_MIME_TO_EXTENSION = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
}


def _parse_issuer(form):
    issuer = (form.get("issuer") or "").strip().upper()
    return issuer if issuer in ISSUER_CHOICES else "HARRASO"


def _next_trip_code(issuer):
    """4 sep, pedido de Braulio: "el codigo de viaje [debe cambiar] de
    acuerdo a empresa. Si es BRMS B-0001 y si es Harraso H-0001" — antes
    todos los viajes compartían un solo correlativo "V-0001" (ver
    next_code en app/helpers.py); ahora cada empresa operadora lleva el
    suyo propio, que nunca se reinicia.

    29 sep, bug encontrado en producción (Braulio, log de Render):
    "psycopg2.errors.UniqueViolation: duplicate key value violates unique
    constraint trips_code_key" al crear un viaje nuevo (o una vuelta).
    Antes este correlativo se sacaba de COUNT(*) de viajes de esa empresa
    -- eso se rompe apenas se borra un viaje que no sea el último (ahora
    posible, ver delete_trip()/_trip_delete_block_reason()): el conteo baja
    y se vuelve a generar un código que ya existe en un viaje que quedó
    vivo. Ahora se toma el número más alto YA USADO en un código de esa
    empresa (sin importar cuántos viajes queden) y se le suma 1 -- nunca
    puede chocar con uno existente, y tampoco se reutiliza un código de un
    viaje borrado."""
    prefix = "B" if issuer == "BRMS" else "H"
    row = query_one(
        "SELECT COALESCE(MAX(CAST(SUBSTR(code, 3) AS INTEGER)), 0) as n FROM trips WHERE issuer = ? AND code LIKE ?",
        (issuer, f"{prefix}-%"),
    )
    n = (row["n"] if row else 0) + 1
    return f"{prefix}-{n:04d}"


def _cargo_types():
    """Tipos de carga (Catálogos → Tipos de carga) -- catálogo editable
    igual que _vehicle_owners() en app/routes/flota.py: se guarda el nombre
    del catálogo tal cual (ej. "Contenedor"), no un código aparte, así que
    agregar o editar un tipo de carga desde Catálogos no necesita ningún
    cambio de código. Devuelve tuplas (nombre, nombre) para no tener que
    tocar el `{% for value, label in cargo_types %}` de viajes/form.html."""
    rows = query_all(
        "SELECT name FROM catalog_items WHERE category = 'cargo_type' AND active = 1 ORDER BY sort_order, name"
    )
    return [(r["name"], r["name"]) for r in rows]


def _parse_cargo_type(form):
    # 30 sep: ya no se valida contra una lista fija en código (ver
    # _cargo_types() arriba) -- mismo criterio que vehicles.owner: cualquier
    # texto no vacío se acepta tal cual, así un viaje que ya tenía un tipo
    # de carga desactivado/renombrado en Catálogos se puede seguir editando
    # sin perder ese dato (ver el <select> de viajes/form.html, que muestra
    # el valor actual aunque ya no esté en el catálogo activo).
    return (form.get("cargo_type") or "").strip() or None


def _parse_container_code(form):
    """Código del contenedor (10 sep, pedido de Braulio) — texto libre, sin
    formato estricto exigido (no todos los clientes usan el estándar ISO
    6346 de 11 caracteres). Se guarda en mayúsculas por consistencia."""
    return (form.get("container_code") or "").strip().upper() or None


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


_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _trip_filters(args):
    """Filtros comunes de los listados de viajes (y de su exportación a Excel):
    estado, búsqueda libre, mes (YYYY-MM sobre la fecha de salida) y conductor
    (cualquiera de los dos en viajes de doble conductor)."""
    month = (args.get("month") or "").strip()
    return {
        "status": args.get("status", ""),
        "q": args.get("q", "").strip(),
        "month": month if _MONTH_RE.match(month) else "",
        "driver": args.get("driver", type=int),
    }


def _filtered_trips(issuer, scope, f):
    """Viajes de `issuer` con unidad propia (scope='propia') o de terceros
    (scope='tercero') que cumplen los filtros `f` (ver _trip_filters), más
    nombres de cliente/unidad/conductores y el código de la ida o vuelta
    enlazada. El mes se compara con substr() de la fecha (texto YYYY-MM-DD)
    para que funcione igual en SQLite y Postgres."""
    ownership = "PROPIA" if scope == "propia" else "TERCERO"
    sql = """SELECT t.*, c.name as client_name, v.plate as vehicle_plate, tv.plate as trailer_plate,
                     d.name as driver_name, d2.name as driver2_name,
                     (SELECT o.code FROM trips o WHERE o.id = t.return_of_trip_id) AS outbound_code,
                     (SELECT r.code FROM trips r WHERE r.return_of_trip_id = t.id) AS return_code
              FROM trips t
              JOIN clients c ON c.id = t.client_id
              LEFT JOIN vehicles v ON v.id = t.vehicle_id
              LEFT JOIN vehicles tv ON tv.id = t.trailer_vehicle_id
              LEFT JOIN drivers d ON d.id = t.driver_id
              LEFT JOIN drivers d2 ON d2.id = t.driver2_id
              WHERE t.ownership = ? AND t.issuer = ?"""
    params = [ownership, issuer]
    if f["status"]:
        sql += " AND t.status = ?"
        params.append(f["status"])
    if f["month"]:
        sql += " AND substr(t.scheduled_date, 1, 7) = ?"
        params.append(f["month"])
    if f["driver"] and scope == "propia":
        sql += " AND (t.driver_id = ? OR t.driver2_id = ?)"
        params += [f["driver"], f["driver"]]
    if f["q"]:
        # LOWER() en ambos lados (patch 0066) -- ver el comentario completo en
        # clientes.list_view().
        sql += """ AND (LOWER(t.code) LIKE LOWER(?) OR LOWER(c.name) LIKE LOWER(?) OR LOWER(t.origin) LIKE LOWER(?)
                         OR LOWER(t.destination) LIKE LOWER(?) OR LOWER(COALESCE(t.third_party_name, '')) LIKE LOWER(?))"""
        params += [f"%{f['q']}%"] * 5
    sql += " ORDER BY t.scheduled_date DESC, t.id DESC"
    return query_all(sql, params)


def _drivers_for_filter():
    return query_all("SELECT id, name FROM drivers ORDER BY name")


@bp.route("")
@permission_required("viajes", "view")
def list_view():
    """3 sep, pedido de Braulio: por defecto el panel general de Viajes solo
    muestra los de unidad propia — los de terceros tienen su propio listado
    (ver list_terceros), con otras columnas relevantes para ese caso.

    16 sep, pedido de Braulio: "separemos tanto los viajes y liquidaciones
    por empresa... debe haber un cuadro arriba de cada uno de sus menus en
    el cual se elija la empresa para evitar confusiones" -- surgió de que
    viajes y liquidaciones usan el mismo prefijo B-/H- pero con
    correlativos independientes (ver _next_trip_code), lo que puede
    confundir si se ven mezclados. Se decidió con Braulio que elegir
    empresa sea OBLIGATORIO (no un filtro opcional con "Todas"): sin
    ?issuer=HARRASO|BRMS en la URL no se consulta ni se muestra ningún
    viaje, solo el selector (ver viajes/list.html)."""
    issuer = request.args.get("issuer", "").strip().upper()
    if issuer not in ISSUER_CHOICES:
        terceros_count = query_one("SELECT COUNT(*) n FROM trips WHERE ownership = 'TERCERO'")["n"]
        return render_template("viajes/list.html", trips=None, issuer=None, terceros_count=terceros_count)

    f = _trip_filters(request.args)
    trips = _filtered_trips(issuer, "propia", f)
    terceros_count = query_one(
        "SELECT COUNT(*) n FROM trips WHERE ownership = 'TERCERO' AND issuer = ?", (issuer,)
    )["n"]
    return render_template(
        "viajes/list.html", trips=trips, issuer=issuer, status=f["status"], q=f["q"], month=f["month"],
        driver=f["driver"], drivers=_drivers_for_filter(), terceros_count=terceros_count,
    )


@bp.route("/terceros")
@permission_required("viajes", "view")
def list_terceros():
    """3 sep, pedido de Braulio: listado aparte para viajes subcontratados a
    terceros, con las columnas que pidió — fecha de viaje, estado, periodo
    de pago y cancelado (sí/no) — además de lo mínimo para identificar cada
    viaje (código, cliente, tercero).

    16 sep: mismo selector de empresa obligatorio que list_view (ver
    comentario ahí)."""
    issuer = request.args.get("issuer", "").strip().upper()
    payment_term_labels = dict(PAYMENT_TERMS)
    if issuer not in ISSUER_CHOICES:
        return render_template(
            "viajes/list_terceros.html", trips=None, issuer=None, payment_term_labels=payment_term_labels
        )

    f = _trip_filters(request.args)
    trips = _filtered_trips(issuer, "tercero", f)
    return render_template(
        "viajes/list_terceros.html",
        trips=trips,
        issuer=issuer,
        status=f["status"],
        q=f["q"],
        month=f["month"],
        payment_term_labels=payment_term_labels,
    )


@bp.route("/exportar")
@permission_required("viajes", "view")
def export_trips():
    """7 oct, pedido de Braulio: "exportar a excel el resumen" -- mismo listado
    y filtros que la pantalla (empresa obligatoria, estado, búsqueda, mes y
    conductor), en un Excel con hoja Resumen y hoja Detalle."""
    from flask import current_app

    from app.reports import build_trips_workbook

    issuer = request.args.get("issuer", "").strip().upper()
    scope = "tercero" if request.args.get("scope") == "tercero" else "propia"
    if issuer not in ISSUER_CHOICES:
        flash("Elige primero la empresa (Harraso o BRMS) para exportar sus viajes.", "error")
        return redirect(url_for("viajes.list_view"))
    f = _trip_filters(request.args)
    trips = _filtered_trips(issuer, scope, f)
    parts = [f"Empresa: {'BRMS' if issuer == 'BRMS' else 'Harraso Transport'}"]
    if f["month"]:
        parts.append(f"Mes: {f['month']}")
    if f["driver"] and scope == "propia":
        drv = query_one("SELECT name FROM drivers WHERE id = ?", (f["driver"],))
        parts.append(f"Conductor: {drv['name'] if drv else f['driver']}")
    if f["status"]:
        parts.append(f"Estado: {f['status'].replace('_', ' ').title()}")
    if f["q"]:
        parts.append(f"Búsqueda: {f['q']}")
    if len(parts) == 1:
        parts.append("Sin más filtros")
    buffer = build_trips_workbook(
        trips, scope, current_app.config["COMPANY_NAME"], " · ".join(parts),
        driver_id=f["driver"] if scope == "propia" else None,
    )
    log_activity("viajes", "EXPORTAR", f"Resumen de viajes a Excel ({' · '.join(parts)}): {len(trips)} viaje(s)")
    name = f"viajes_{'terceros_' if scope == 'tercero' else ''}{issuer.lower()}{'_' + f['month'] if f['month'] else ''}.xlsx"
    return Response(
        buffer.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={name}"},
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
    desplegable al editar un viaje viejo (mismo criterio que ya se usaba).

    15 sep, pedido de Braulio: una unidad en mantenimiento también aparece
    acá si fue marcada "disponible para programar" (available_for_scheduling
    — solo Administrador/Mecánico pueden marcarla, ver Mantenimiento -> Por
    unidad). Sigue habiendo un chequeo al guardar el viaje (ver
    _vehicle_maintenance_error()) por si la unidad deja de estar disponible
    entre que se cargó el formulario y se envía."""
    return query_all(
        """SELECT * FROM vehicles
           WHERE (vehicle_type != 'CARRETA' AND (status = 'ACTIVO' OR (status = 'MANTENIMIENTO' AND available_for_scheduling = 1)))
           OR id = ? ORDER BY plate""",
        (current_vehicle_id,),
    )


def _active_trailers(current_trailer_id=None):
    """Carretas (semirremolques) disponibles para el campo "Carreta" del
    formulario de viajes (3 sep, pedido de Braulio: "se debe seleccionar
    tanto la unidad tracto como la carreta"). Mismo criterio de "disponible
    para programar" que _active_vehicles() (15 sep) -- una carreta es una
    fila más de "vehicles", con el mismo status/flag."""
    return query_all(
        """SELECT * FROM vehicles
           WHERE (vehicle_type = 'CARRETA' AND (status = 'ACTIVO' OR (status = 'MANTENIMIENTO' AND available_for_scheduling = 1)))
           OR id = ? ORDER BY plate""",
        (current_trailer_id,),
    )


def _vehicle_maintenance_error(vehicle_id):
    """15 sep, pedido de Braulio: "Si no tiene marcada la opcion de
    disponible para programar, le debe salir a la hora de querer asignar
    la unidad a un viaje en rojo con el mensaje unidad en mantenimiento."
    Devuelve el mensaje de error (se agrega a la lista `errors` del
    formulario, que ya se muestra en rojo vía flash "error" — mismo
    mecanismo que cualquier otro error de validación) o None si la unidad
    está bien. _active_vehicles()/_active_trailers() ya excluyen del
    desplegable a una unidad en mantenimiento sin este flag, pero este
    chequeo es la defensa real -- cubre el caso de una unidad que ya
    estaba asignada a un viaje y entró a mantenimiento después (sigue en
    el desplegable al editar ese viaje, ver el "OR id = ?" de arriba), y
    cualquier envío del formulario con un vehicle_id manipulado a mano."""
    if not vehicle_id:
        return None
    vehicle = query_one("SELECT plate, status, available_for_scheduling FROM vehicles WHERE id = ?", (vehicle_id,))
    if vehicle is None or vehicle["status"] != "MANTENIMIENTO" or vehicle["available_for_scheduling"]:
        return None
    return f'Unidad en mantenimiento: "{vehicle["plate"]}" no fue marcada como disponible para programar (solo Administrador o Mantenimiento pueden hacerlo, en Mantenimiento → Por unidad).'


def _vehicle_open_orders_warning(vehicle_id):
    """15 sep, pedido de Braulio: "Si aun tiene sigue en estado en
    proceso, debe salir una alerta al operador... indicando que la unidad
    aun tiene ordenes de trabajo abiertas, las cuales deben ser atendidas
    cuanto antes." Se avisa (no bloquea, a diferencia de
    _vehicle_maintenance_error()) cuando la unidad SÍ está disponible para
    programar pero todavía tiene una orden de mantenimiento sin terminar
    -- "abierta" replica el mismo criterio que _order_status() en
    app/routes/mantenimiento.py (SIN_TRABAJOS/PENDIENTE/EN_PROCESO, osea
    cualquier cosa que no sea TERMINADA), reescrito acá en SQL para no
    importar entre módulos de rutas."""
    if not vehicle_id:
        return None
    vehicle = query_one("SELECT plate, status, available_for_scheduling FROM vehicles WHERE id = ?", (vehicle_id,))
    if vehicle is None or vehicle["status"] != "MANTENIMIENTO" or not vehicle["available_for_scheduling"]:
        return None
    open_order = query_one(
        """SELECT m.id FROM maintenance_records m
           WHERE m.vehicle_id = ?
           AND (
               NOT EXISTS (SELECT 1 FROM maintenance_record_jobs j WHERE j.maintenance_record_id = m.id)
               OR EXISTS (SELECT 1 FROM maintenance_record_jobs j WHERE j.maintenance_record_id = m.id AND j.status != 'TERMINADO')
           )
           LIMIT 1""",
        (vehicle_id,),
    )
    if open_order is None:
        return None
    return f'La unidad "{vehicle["plate"]}" todavía tiene órdenes de trabajo abiertas en Mantenimiento — deben atenderse cuanto antes.'


def _vehicles_with_open_maintenance_orders():
    """15 sep, pedido de Braulio (ajuste): la alerta de "trabajos
    pendientes a atender a su retorno" ya no debe salir recién después de
    guardar el viaje (ver _vehicle_open_orders_warning, que sigue estando
    como respaldo del lado del servidor) sino apenas el operador ELIGE la
    unidad en el desplegable, como un aviso que hay que cerrar para poder
    seguir. Esto se hace con JS en viajes/form.html, así que acá se arma
    el conjunto de ids de TODAS las unidades con al menos una orden de
    mantenimiento "abierta" (mismo criterio que _vehicle_open_orders_warning
    -- sin trabajos cargados, o con algún trabajo que no esté TERMINADO) y
    se lo pasa al formulario para que el JS lo consulte por unidad, sin
    tener que ir al servidor por cada cambio de selección."""
    rows = query_all(
        """SELECT DISTINCT m.vehicle_id FROM maintenance_records m
           WHERE NOT EXISTS (SELECT 1 FROM maintenance_record_jobs j WHERE j.maintenance_record_id = m.id)
              OR EXISTS (SELECT 1 FROM maintenance_record_jobs j WHERE j.maintenance_record_id = m.id AND j.status != 'TERMINADO')"""
    )
    return [r["vehicle_id"] for r in rows]


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


def _return_trip_of(trip_id):
    """29 sep, pedido de Braulio ("los viajes de ambas empresas contienen un
    ida y vuelta... debemos tener 2 pantallas"): el viaje de vuelta de un
    viaje de ida dado, si ya se creó -- ver return_of_trip_id en
    schema.sql y new_return_trip() más abajo."""
    return query_one(
        "SELECT id, code, status FROM trips WHERE return_of_trip_id = ?", (trip_id,)
    )


def _advance_for_driver(trip_id, driver_id):
    """30 sep, pedido de Braulio: "cuando el viaje es doble conductor
    tambien se debe poder registrar liquidacion del segundo conductor" --
    ahora un mismo trip_id puede tener hasta dos liquidaciones (una por
    conductor, ver expense_advances.driver_id en schema.sql), así que ya no
    alcanza con mirar solo trip_id (ver detail() más abajo). NULL-safe: si
    el viaje no tiene ese conductor asignado, compara por "IS NULL" (en SQL
    "= NULL" nunca es verdadero)."""
    if driver_id is not None:
        return query_one(
            "SELECT id, status FROM expense_advances WHERE trip_id = ? AND driver_id = ?", (trip_id, driver_id)
        )
    return query_one(
        "SELECT id, status FROM expense_advances WHERE trip_id = ? AND driver_id IS NULL", (trip_id,)
    )


def liquidation_anchor_trip_id(trip_id):
    """5 oct, pedido de Braulio (viajes H-0039 ida / H-0040 vuelta):
    "esta jalando los gastos del h-0040, son doble conductor pero quedamos
    que cada uno liquidaba sus gastos de manera independiente".

    Desde el 29 sep la liquidación (anticipo + gastos) de un viaje redondo
    era SIEMPRE una sola, anclada al viaje de IDA (trip_id de la ida) -- así
    cada gasto registrado desde la pantalla de la vuelta terminaba guardado
    (y mostrado) en la ida. Eso tiene sentido cuando UN solo conductor hace
    todo el viaje redondo, pero no cuando es doble conductor: ahí cada
    conductor liquida lo suyo, de forma independiente (ver 30 sep, una
    liquidación por conductor) y la ida y la vuelta no deben mezclar gastos.

    Devuelve el trip_id donde viven el anticipo y los gastos de `trip_id`:
    - una ida (o un viaje normal): él mismo, siempre.
    - una vuelta: el id de la ida SOLO si ambos tramos son de un único
      conductor y es el mismo (ahí sí se sigue compartiendo una sola
      liquidación, como desde el 29 sep). Si cualquiera de los dos tramos
      es doble conductor, o los conductores de ida y vuelta son distintos
      (o falta alguno), la vuelta liquida por su cuenta: devuelve su propio
      id."""
    trip = query_one(
        "SELECT id, return_of_trip_id, driver_id, double_driver, separate_liquidation FROM trips WHERE id = ?", (trip_id,)
    )
    if trip is None:
        return trip_id
    outbound_id = trip["return_of_trip_id"]
    if not outbound_id:
        return trip["id"]
    if trip["separate_liquidation"]:
        # 7 oct: vuelta enlazada a mano que ya traía su propia liquidación
        # (ver link_trips()): sigue liquidando por su cuenta.
        return trip["id"]
    outbound = query_one("SELECT id, driver_id, double_driver FROM trips WHERE id = ?", (outbound_id,))
    if outbound is None:
        return trip["id"]
    if trip["double_driver"] or outbound["double_driver"]:
        return trip["id"]
    if not trip["driver_id"] or trip["driver_id"] != outbound["driver_id"]:
        return trip["id"]
    return outbound["id"]


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
        else:
            maintenance_error = _vehicle_maintenance_error(fields["vehicle_id"])
            if maintenance_error:
                errors.append(maintenance_error)
        if not fields["trailer_vehicle_id"]:
            errors.append("Selecciona la carreta.")
        else:
            maintenance_error = _vehicle_maintenance_error(fields["trailer_vehicle_id"])
            if maintenance_error:
                errors.append(maintenance_error)
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


def _add_creation_guides(trip_id, code):
    """5 oct, pedido de Braulio: la guía del remitente y la guía de
    transportista (nuestra, emitida en otro portal) se pueden subir desde
    la creación del viaje (también la de un viaje de vuelta) -- ambas
    opcionales. Se pueden agregar más después desde el detalle del viaje,
    sin reemplazar estas."""
    added = []
    if add_trip_guide(trip_id, "REMITENTE", request.form.get("shipper_waybill_number"), request.files.get("shipper_waybill_file")):
        added.append("remitente")
    if add_trip_guide(trip_id, "TRANSPORTISTA", request.form.get("carrier_waybill_number"), request.files.get("carrier_waybill_file")):
        added.append("transportista")
    if added:
        log_activity(
            "viajes", "SUBIR", f"Viaje {code}: guía de {' y de '.join(added)} adjuntada al crear",
            entity_type="viaje", entity_id=trip_id,
            entity_url=url_for("viajes.detail", trip_id=trip_id),
        )


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
        # Código de contenedor + foto de evidencia (10 sep) — solo aplican a
        # viajes de tipo CONTENEDOR; se limpian a None para cualquier otro
        # tipo de carga aunque el formulario los haya enviado (mismo
        # criterio que los campos de propia/tercero en
        # _ownership_and_third_party_fields).
        # OJO: depende del nombre exacto "Contenedor" en el catálogo Tipos
        # de carga -- si se renombra desde Catálogos, los viajes NUEVOS de
        # ese tipo dejan de activar estos campos (ver el comentario largo
        # junto a trips.cargo_type en schema.sql).
        if cargo_type == "Contenedor":
            container_code = _parse_container_code(request.form)
            container_photo_filename = _save_container_photo_file(request.files.get("container_photo"))
        else:
            container_code = None
            container_photo_filename = None
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
                cargo_types=_cargo_types(), payment_terms=PAYMENT_TERMS,
                open_maintenance_vehicle_ids=_vehicles_with_open_maintenance_orders(),
            )

        code = _next_trip_code(issuer)
        driver_commission = _resolve_commission(
            request.form, origin, destination, route,
            double_driver=double_driver, single_leg=single_leg,
        )
        trip_id = execute(
            """INSERT INTO trips (code, client_id, vehicle_id, trailer_vehicle_id, driver_id, driver2_id,
               origin, destination, cargo_description, cargo_weight_kg, cargo_type, container_code,
               container_photo_filename, scheduled_date, rate,
               driver_commission, double_driver, single_leg, notes, issuer, ownership,
               third_party_name, third_party_unit, third_party_rate, third_party_payment_term, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                container_code,
                container_photo_filename,
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
        log_activity(
            "viajes", "CREAR", f"Viaje {code} ({origin} → {destination})",
            entity_type="viaje", entity_id=trip_id,
            entity_url=url_for("viajes.detail", trip_id=trip_id),
        )
        flash(f"Viaje {code} creado.", "success")
        _add_creation_guides(trip_id, code)
        for w in (
            _vehicle_open_orders_warning(ownership_fields["vehicle_id"]),
            _vehicle_open_orders_warning(ownership_fields["trailer_vehicle_id"]),
        ):
            if w:
                flash(w, "info")
        return redirect(url_for("viajes.detail", trip_id=trip_id))

    # Llega desde el paso 1 (empresa/unidad ya elegidos en la query string) —
    # se preseleccionan en el paso 2 y quedan fijos para esta creación (ver
    # viajes/form.html, bloque `mode == 'new'`).
    return render_template(
        "viajes/form.html", trip=None, mode="new",
        clients=clients, vehicles=vehicles, trailers=trailers, drivers=drivers, routes=routes, today=today_str(),
        selected_route_id="", cargo_types=_cargo_types(), payment_terms=PAYMENT_TERMS,
        preset_issuer=_parse_issuer(request.args), preset_ownership=_parse_ownership(request.args),
        open_maintenance_vehicle_ids=_vehicles_with_open_maintenance_orders(),
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
        # Código de contenedor + foto de evidencia (10 sep) — igual que en
        # new(): solo aplican a CONTENEDOR. Si ya había una foto y no se
        # sube una nueva, se conserva la anterior (mismo criterio que
        # save_waybill()/save_delivery_proof()); si el tipo de carga deja de
        # ser CONTENEDOR, se limpian ambos campos.
        # OJO: depende del nombre exacto "Contenedor" en el catálogo Tipos
        # de carga -- si se renombra desde Catálogos, los viajes NUEVOS de
        # ese tipo dejan de activar estos campos (ver el comentario largo
        # junto a trips.cargo_type en schema.sql).
        if cargo_type == "Contenedor":
            container_code = _parse_container_code(request.form)
            new_container_photo = _save_container_photo_file(request.files.get("container_photo"))
            container_photo_filename = new_container_photo if new_container_photo else trip["container_photo_filename"]
        else:
            container_code = None
            container_photo_filename = None
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
        # 4 sep, pedido de Braulio: los viajes con terceros no registran
        # liquidación — si este viaje (antes PROPIA) ya tiene una liquidación,
        # gastos o una inspección registrada, no se deja pasar a TERCERO sin
        # que Braulio decida primero qué hacer con esos registros (quedarían
        # ocultos del detalle del viaje, ver viajes/detail.html).
        if ownership_fields["ownership"] == "TERCERO" and trip["ownership"] != "TERCERO":
            has_liquidacion_data = query_one(
                """SELECT
                       (SELECT COUNT(*) FROM expense_advances WHERE trip_id = ?)
                     + (SELECT COUNT(*) FROM expenses WHERE trip_id = ?)
                     + (SELECT COUNT(*) FROM inspections WHERE trip_id = ?) as n""",
                (trip_id, trip_id, trip_id),
            )["n"]
            if has_liquidacion_data:
                errors.append(
                    "Este viaje ya tiene liquidación, gastos o una inspección registrada — "
                    "no se puede pasar a Tercero sin resolver esos registros primero."
                )
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "viajes/form.html", trip=trip, mode="edit", trip_id=trip_id,
                clients=clients, vehicles=vehicles, trailers=trailers, drivers=drivers, routes=routes,
                selected_route_id=request.form.get("route_id", ""),
                cargo_types=_cargo_types(), payment_terms=PAYMENT_TERMS,
                open_maintenance_vehicle_ids=_vehicles_with_open_maintenance_orders(),
            )
        driver_commission = _resolve_commission(
            request.form, origin, destination, route,
            double_driver=double_driver, single_leg=single_leg,
        )
        execute(
            """UPDATE trips SET client_id=?, vehicle_id=?, trailer_vehicle_id=?, driver_id=?, driver2_id=?,
               origin=?, destination=?, cargo_description=?, cargo_weight_kg=?, cargo_type=?, container_code=?,
               container_photo_filename=?, scheduled_date=?,
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
                container_code,
                container_photo_filename,
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
        log_activity(
            "viajes", "EDITAR", f"Viaje {trip['code']} ({origin} → {destination})",
            entity_type="viaje", entity_id=trip_id,
            entity_url=url_for("viajes.detail", trip_id=trip_id),
        )
        flash("Viaje actualizado.", "success")
        for w in (
            _vehicle_open_orders_warning(ownership_fields["vehicle_id"]),
            _vehicle_open_orders_warning(ownership_fields["trailer_vehicle_id"]),
        ):
            if w:
                flash(w, "info")
        return redirect(url_for("viajes.detail", trip_id=trip_id))

    return render_template(
        "viajes/form.html", trip=trip, mode="edit", trip_id=trip_id,
        clients=clients, vehicles=vehicles, trailers=trailers, drivers=drivers, routes=routes,
        selected_route_id=_selected_route_id_for_edit(trip),
        cargo_types=_cargo_types(), payment_terms=PAYMENT_TERMS,
        open_maintenance_vehicle_ids=_vehicles_with_open_maintenance_orders(),
    )


@bp.route("/<int:trip_id>/vuelta/nuevo", methods=["GET", "POST"])
@permission_required("viajes", "edit")
def new_return_trip(trip_id):
    """29 sep, pedido de Braulio ("Los viajes de ambas empresas continenen
    un ida y vuelta... debemos tener 2 pantallas"): crea el viaje de VUELTA
    como un viaje de la tabla trips más -- su propio código, factura,
    comisión, GPS, inspecciones, guías, conformidad de entrega, etc. -- en
    vez de duplicar todos esos campos en el mismo registro de la ida (ver
    el comentario largo junto a return_of_trip_id en schema.sql). Cliente,
    tipo de carga, conductor(es) y unidad/tercero se precargan iguales a
    los del viaje de ida pero quedan editables (pedido explícito de
    Braulio); origen/destino se intercambian respecto a la ida y se intenta
    preseleccionar la ruta inversa del catálogo si existe. Tarifa y
    comisión NO se precargan -- la vuelta se factura y comisiona aparte,
    como cualquier viaje nuevo (se sugieren según la ruta elegida)."""
    outbound = query_one("SELECT * FROM trips WHERE id = ?", (trip_id,))
    if outbound is None:
        abort(404)
    if outbound["return_of_trip_id"]:
        flash("Este viaje ya es una vuelta -- no se puede crear una vuelta de una vuelta.", "error")
        return redirect(url_for("viajes.detail", trip_id=trip_id))
    existing_return = _return_trip_of(trip_id)
    if existing_return:
        flash(f"Este viaje ya tiene un viaje de vuelta: {existing_return['code']}.", "info")
        return redirect(url_for("viajes.detail", trip_id=existing_return["id"]))

    clients = query_all("SELECT * FROM clients WHERE active = 1 ORDER BY name")
    vehicles = _active_vehicles(outbound["vehicle_id"])
    trailers = _active_trailers(outbound["trailer_vehicle_id"])
    drivers = query_all(
        "SELECT * FROM drivers WHERE status = 'ACTIVO' OR id = ? OR id = ? ORDER BY name",
        (outbound["driver_id"], outbound["driver2_id"]),
    )
    routes = _active_routes()
    # Ruta preseleccionada: la inversa de la ida (destino → origen), si ya
    # está en el catálogo activo -- si no, el despachador debe elegir una
    # (igual que cualquier viaje nuevo, ver _resolve_route_selection()).
    reverse_route = find_route(outbound["destination"], outbound["origin"])
    default_route_id = str(reverse_route["id"]) if reverse_route else ""

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
        issuer = outbound["issuer"]  # heredado de la ida, no editable -- ver viajes/form.html modo "vuelta"
        cargo_type = _parse_cargo_type(request.form)
        # OJO: depende del nombre exacto "Contenedor" en el catálogo Tipos
        # de carga -- si se renombra desde Catálogos, los viajes NUEVOS de
        # ese tipo dejan de activar estos campos (ver el comentario largo
        # junto a trips.cargo_type en schema.sql).
        if cargo_type == "Contenedor":
            container_code = _parse_container_code(request.form)
            container_photo_filename = _save_container_photo_file(request.files.get("container_photo"))
        else:
            container_code = None
            container_photo_filename = None
        ownership_fields, ownership_errors = _ownership_and_third_party_fields(request.form)
        errors = list(ownership_errors)
        if not client_id:
            errors.append("Selecciona un cliente.")
        if route_error:
            errors.append(route_error)
        if not scheduled_date:
            errors.append("La fecha de salida no es válida.")
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
                "viajes/form.html", trip=request.form, mode="vuelta", outbound_trip=outbound,
                clients=clients, vehicles=vehicles, trailers=trailers, drivers=drivers, routes=routes,
                selected_route_id=request.form.get("route_id", ""),
                cargo_types=_cargo_types(), payment_terms=PAYMENT_TERMS,
                open_maintenance_vehicle_ids=_vehicles_with_open_maintenance_orders(),
            )

        code = _next_trip_code(issuer)
        driver_commission = _resolve_commission(
            request.form, origin, destination, route,
            double_driver=double_driver, single_leg=single_leg,
        )
        new_trip_id = execute(
            """INSERT INTO trips (code, client_id, vehicle_id, trailer_vehicle_id, driver_id, driver2_id,
               return_of_trip_id, origin, destination, cargo_description, cargo_weight_kg, cargo_type,
               container_code, container_photo_filename, scheduled_date, rate,
               driver_commission, double_driver, single_leg, notes, issuer, ownership,
               third_party_name, third_party_unit, third_party_rate, third_party_payment_term, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                code,
                client_id,
                ownership_fields["vehicle_id"],
                ownership_fields["trailer_vehicle_id"],
                driver_id,
                driver2_id,
                trip_id,
                origin,
                destination,
                request.form.get("cargo_description", "").strip(),
                parse_float(request.form.get("cargo_weight_kg"), None),
                cargo_type,
                container_code,
                container_photo_filename,
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
        log_activity(
            "viajes", "CREAR", f"Viaje {code} ({origin} → {destination}) — vuelta de {outbound['code']}",
            entity_type="viaje", entity_id=new_trip_id,
            entity_url=url_for("viajes.detail", trip_id=new_trip_id),
        )
        flash(f"Viaje de vuelta {code} creado.", "success")
        _add_creation_guides(new_trip_id, code)
        for w in (
            _vehicle_open_orders_warning(ownership_fields["vehicle_id"]),
            _vehicle_open_orders_warning(ownership_fields["trailer_vehicle_id"]),
        ):
            if w:
                flash(w, "info")
        return redirect(url_for("viajes.detail", trip_id=new_trip_id))

    prefill = {
        "client_id": outbound["client_id"],
        "issuer": outbound["issuer"],
        "ownership": outbound["ownership"],
        "vehicle_id": outbound["vehicle_id"],
        "trailer_vehicle_id": outbound["trailer_vehicle_id"],
        "driver_id": outbound["driver_id"],
        "driver2_id": outbound["driver2_id"],
        "double_driver": outbound["double_driver"],
        "single_leg": outbound["single_leg"],
        "cargo_type": outbound["cargo_type"],
        "container_code": outbound["container_code"],
        "third_party_name": outbound["third_party_name"],
        "third_party_unit": outbound["third_party_unit"],
        "third_party_rate": outbound["third_party_rate"],
        "third_party_payment_term": outbound["third_party_payment_term"],
        "scheduled_date": today_str(),
        "rate": 0,
    }
    return render_template(
        "viajes/form.html", trip=prefill, mode="vuelta", outbound_trip=outbound,
        clients=clients, vehicles=vehicles, trailers=trailers, drivers=drivers, routes=routes, today=today_str(),
        selected_route_id=default_route_id, cargo_types=_cargo_types(), payment_terms=PAYMENT_TERMS,
        open_maintenance_vehicle_ids=_vehicles_with_open_maintenance_orders(),
    )


# --- Enlazar dos viajes ya creados como ida y vuelta -------------------------

def _trip_has_liquidation_data(trip_id):
    """¿Este viaje ya tiene anticipo de viáticos o gastos registrados a su
    nombre? (Esa data vive en su propio trip_id, ver liquidation_anchor_trip_id.)"""
    return bool(
        query_one("SELECT id FROM expense_advances WHERE trip_id = ? LIMIT 1", (trip_id,))
        or query_one("SELECT id FROM expenses WHERE trip_id = ? LIMIT 1", (trip_id,))
    )


def _would_share_liquidation(ida, vuelta):
    """Con las reglas de liquidation_anchor_trip_id(): ¿ida y vuelta
    compartirían UNA liquidación? (un solo conductor, el mismo en ambos)."""
    if ida["double_driver"] or vuelta["double_driver"]:
        return False
    return bool(vuelta["driver_id"]) and vuelta["driver_id"] == ida["driver_id"]


def _link_error(ida, vuelta):
    """Motivo por el que `vuelta` no puede enlazarse como vuelta de `ida`
    (None si sí se puede)."""
    if ida is None or vuelta is None:
        return "No se encontró uno de los viajes."
    if ida["id"] == vuelta["id"]:
        return "No puedes enlazar un viaje consigo mismo."
    if ida["issuer"] != vuelta["issuer"]:
        return "Los dos viajes deben ser de la misma empresa (Harraso o BRMS)."
    if "CANCELADO" in (ida["status"], vuelta["status"]):
        return "No se pueden enlazar viajes cancelados."
    if ida["return_of_trip_id"]:
        return f"{ida['code']} ya es la vuelta de otro viaje: no puede ser la ida."
    if vuelta["return_of_trip_id"]:
        return f"{vuelta['code']} ya es la vuelta de otro viaje."
    existing = _return_trip_of(ida["id"])
    if existing:
        return f"{ida['code']} ya tiene un viaje de vuelta ({existing['code']})."
    own_return = _return_trip_of(vuelta["id"])
    if own_return:
        return f"{vuelta['code']} ya tiene su propia vuelta ({own_return['code']}): no puede ser a la vez una vuelta."
    return None


def _day_gap(a, b):
    from datetime import datetime

    try:
        return abs((datetime.strptime(a[:10], "%Y-%m-%d") - datetime.strptime(b[:10], "%Y-%m-%d")).days)
    except (TypeError, ValueError):
        return 9999


@bp.route("/<int:trip_id>/enlazar", methods=["GET", "POST"])
@permission_required("viajes", "edit")
def link_trips(trip_id):
    """7 oct, pedido de Braulio: "crear la opción de enlazar 2 viajes ya
    creados, uno se convierta en la vuelta". `rol=ida` (por defecto): este
    viaje es la ida y se elige cuál ya creado será su vuelta. `rol=vuelta`:
    este viaje es la vuelta y se elige su ida. En ambos casos queda igual que
    una vuelta creada con "Crear viaje de vuelta": return_of_trip_id en la
    vuelta (ver schema.sql). La liquidación (anticipo y gastos) se comparte si
    hay un solo conductor en ambos tramos; si la vuelta ya traía anticipo o
    gastos propios (o se pide a mano) queda independiente
    (trips.separate_liquidation) para no mover liquidaciones ya hechas."""
    trip = query_one("SELECT * FROM trips WHERE id = ?", (trip_id,))
    if trip is None:
        abort(404)
    role = (request.values.get("rol") or "ida").strip().lower()
    if role not in ("ida", "vuelta"):
        role = "ida"
    back = redirect(url_for("viajes.detail", trip_id=trip_id))
    if trip["return_of_trip_id"]:
        flash(f"{trip['code']} ya es una vuelta: no se puede enlazar de nuevo.", "error")
        return back
    existing = _return_trip_of(trip_id)
    if existing:
        flash(f"{trip['code']} ya tiene un viaje de vuelta ({existing['code']}).", "info")
        return back
    if trip["status"] == "CANCELADO":
        flash("No se pueden enlazar viajes cancelados.", "error")
        return back

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        other = query_one("SELECT * FROM trips WHERE id = ?", (request.form.get("other_id", type=int),))
        if other is None:
            flash("Elige el viaje que quieres enlazar.", "error")
            return redirect(url_for("viajes.link_trips", trip_id=trip_id, rol=role))
        ida, vuelta = (trip, other) if role == "ida" else (other, trip)
        err = _link_error(ida, vuelta)
        if err:
            flash(err, "error")
            return redirect(url_for("viajes.link_trips", trip_id=trip_id, rol=role))
        separate = 0
        if _would_share_liquidation(ida, vuelta):
            if _trip_has_liquidation_data(vuelta["id"]) or request.form.get("separate_liquidation"):
                separate = 1
        execute(
            "UPDATE trips SET return_of_trip_id = ?, separate_liquidation = ? WHERE id = ?",
            (ida["id"], separate, vuelta["id"]),
        )
        log_activity(
            "viajes", "EDITAR", f"Viaje {vuelta['code']} enlazado como vuelta de {ida['code']}",
            entity_type="viaje", entity_id=vuelta["id"], entity_url=url_for("viajes.detail", trip_id=vuelta["id"]),
        )
        msg = f"Listo: {vuelta['code']} quedó como la vuelta de {ida['code']}."
        if separate:
            msg += " Cada uno conserva su propia liquidación (anticipo y gastos)."
        flash(msg, "success")
        return back

    q = request.args.get("q", "").strip()
    show_all = bool(request.args.get("todos"))
    sql = """SELECT t.*, c.name AS client_name, d.name AS driver_name, d2.name AS driver2_name, v.plate AS vehicle_plate
             FROM trips t JOIN clients c ON c.id = t.client_id
             LEFT JOIN drivers d ON d.id = t.driver_id LEFT JOIN drivers d2 ON d2.id = t.driver2_id
             LEFT JOIN vehicles v ON v.id = t.vehicle_id
             WHERE t.id != ? AND t.issuer = ? AND t.status != 'CANCELADO' AND t.return_of_trip_id IS NULL
               AND NOT EXISTS (SELECT 1 FROM trips r WHERE r.return_of_trip_id = t.id)"""
    params = [trip_id, trip["issuer"]]
    if q:
        sql += """ AND (LOWER(t.code) LIKE LOWER(?) OR LOWER(c.name) LIKE LOWER(?) OR LOWER(t.origin) LIKE LOWER(?)
                        OR LOWER(t.destination) LIKE LOWER(?) OR LOWER(COALESCE(d.name, '')) LIKE LOWER(?))"""
        params += [f"%{q}%"] * 5
    sql += " ORDER BY t.scheduled_date DESC, t.id DESC LIMIT 400"
    candidates = []
    for t in query_all(sql, params):
        gap = _day_gap(t["scheduled_date"], trip["scheduled_date"])
        if not show_all and not q and gap > 45:
            continue
        ida, vuelta = (trip, t) if role == "ida" else (t, trip)
        candidates.append({
            "t": t, "gap": gap,
            "reverse": t["origin"] == trip["destination"] and t["destination"] == trip["origin"],
            "same_driver": bool(t["driver_id"]) and t["driver_id"] == trip["driver_id"],
            "order_ok": vuelta["scheduled_date"] >= ida["scheduled_date"],
            "has_liquidation": _trip_has_liquidation_data(t["id"]),
            "share": _would_share_liquidation(ida, vuelta),
        })
    candidates.sort(key=lambda c: (not c["reverse"], not c["same_driver"], c["gap"]))
    return render_template(
        "viajes/enlazar.html", trip=trip, role=role, candidates=candidates[:80], q=q, show_all=show_all,
        trip_has_liquidation=_trip_has_liquidation_data(trip_id),
    )


@bp.route("/<int:trip_id>/desenlazar", methods=["POST"])
@permission_required("viajes", "edit")
def unlink_trips(trip_id):
    """Deshace el enlace ida/vuelta (se llama desde cualquiera de los dos
    viajes). Si ida y vuelta comparten una liquidación que ya tiene anticipo
    o gastos, no se deshace: esos gastos son de los dos tramos a la vez y no
    se pueden repartir solos."""
    if not validate_csrf():
        abort(400)
    trip = query_one("SELECT * FROM trips WHERE id = ?", (trip_id,))
    if trip is None:
        abort(404)
    back = redirect(url_for("viajes.detail", trip_id=trip_id))
    if trip["return_of_trip_id"]:
        vuelta = trip
        ida = query_one("SELECT * FROM trips WHERE id = ?", (trip["return_of_trip_id"],))
    else:
        ida = trip
        vuelta = query_one("SELECT * FROM trips WHERE return_of_trip_id = ?", (trip_id,))
    if ida is None or vuelta is None:
        flash("Este viaje no está enlazado como ida/vuelta.", "info")
        return back
    if liquidation_anchor_trip_id(vuelta["id"]) == ida["id"] and _trip_has_liquidation_data(ida["id"]):
        flash(
            f"No se puede deshacer: {ida['code']} y {vuelta['code']} comparten una liquidación que ya tiene "
            "anticipo o gastos, y esos montos son de los dos tramos. Elimina o separa esa liquidación primero.",
            "error",
        )
        return back
    execute("UPDATE trips SET return_of_trip_id = NULL, separate_liquidation = 0 WHERE id = ?", (vuelta["id"],))
    log_activity(
        "viajes", "EDITAR", f"Viaje {vuelta['code']} dejó de ser la vuelta de {ida['code']}",
        entity_type="viaje", entity_id=vuelta["id"], entity_url=url_for("viajes.detail", trip_id=vuelta["id"]),
    )
    flash(f"Se deshizo el enlace: {vuelta['code']} y {ida['code']} ahora son viajes independientes.", "success")
    return back


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
    # 29 sep, pedido de Braulio (ida/vuelta): "ambos [ida y vuelta] igual
    # estan siendo amarrados a la misma liquidacion" -- confirmó que
    # quiere UNA sola liquidación (anticipo de viáticos + gastos) para el
    # viaje redondo completo, a diferencia de factura/comisión (esas sí
    # quedan separadas, cada una se factura aparte -- ver
    # new_return_trip()). liquidacion_trip_id es el "ancla" de esa
    # liquidación compartida: siempre el id del viaje de IDA, sin importar
    # si se está viendo la pantalla de la ida o la de la vuelta -- así
    # ambas pantallas leen/escriben el mismo anticipo y la misma lista de
    # gastos (ver también los links de "Confirmar anticipo"/"Registrar
    # gasto" en viajes/detail.html, que usan este mismo id).
    # 5 oct, pedido de Braulio: ya no es "siempre la ida" -- ver
    # liquidation_anchor_trip_id(): si es doble conductor (o los conductores
    # de ida y vuelta no son el mismo único conductor), cada viaje liquida
    # por su cuenta y su propio id es el ancla.
    liquidacion_trip_id = liquidation_anchor_trip_id(trip["id"])
    expenses = query_all(
        "SELECT * FROM expenses WHERE trip_id = ? ORDER BY expense_date DESC", (liquidacion_trip_id,)
    )
    total_expenses = sum(e["amount"] for e in expenses)
    next_statuses = STATUS_FLOW.get(trip["status"], [])
    # 30 sep, pedido de Braulio: "cuando el viaje es doble conductor tambien
    # se debe poder registrar liquidacion del segundo conductor" -- de acá
    # en más un viaje puede tener DOS liquidaciones (una por conductor), así
    # que hace falta el driver_id/driver2_id del viaje ANCLA (la ida, si se
    # está viendo la vuelta -- ver el comentario de liquidacion_trip_id
    # arriba) para saber cuál liquidación corresponde a cuál conductor, en
    # vez de los driver_id/driver2_id de ESTE trip (que podrían ser
    # distintos si se está viendo la pantalla de la vuelta).
    anchor_trip_info = query_one(
        """SELECT t.driver_id, t.driver2_id, t.double_driver,
                  d.name as driver_name, d2.name as driver2_name
           FROM trips t
           LEFT JOIN drivers d ON d.id = t.driver_id
           LEFT JOIN drivers d2 ON d2.id = t.driver2_id
           WHERE t.id = ?""",
        (liquidacion_trip_id,),
    )
    advance = _advance_for_driver(liquidacion_trip_id, anchor_trip_info["driver_id"])
    advance2 = None
    if anchor_trip_info["double_driver"] and anchor_trip_info["driver2_id"]:
        advance2 = _advance_for_driver(liquidacion_trip_id, anchor_trip_info["driver2_id"])
    payment_term_labels = dict(PAYMENT_TERMS)
    # 30 sep: ya no hace falta un diccionario de etiquetas para el tipo de
    # carga -- ahora que se guarda el nombre del catálogo tal cual (ver
    # _cargo_types() arriba), trip.cargo_type YA ES la etiqueta a mostrar
    # (ver viajes/detail.html).
    # 15 sep, pedido de Braulio: si el viaje ya tiene una guía de
    # transportista generada (módulo Guías), la pregunta de "¿la guía del
    # remitente ya figura con nuestros datos?" ya no aplica -- no se le
    # vuelve a pedir la respuesta a un viaje ya procesado.
    # 1 oct, pedido de Braulio ("ya se creo la guia de este viaje, pero
    # cuando lo abro no la puedo ver"): existing_waybills ya se consultaba
    # para decidir si mostrar el botón "Generar guía de remisión" (ver más
    # abajo en viajes/detail.html), pero nunca se listaba -- una vez
    # generada, no había forma de verla/descargarla desde el detalle del
    # viaje (había que ir a Guías de Remisión y buscarla a mano). Se agrega
    # sunat_pdf_url para poder enlazar el PDF directo cuando ya fue
    # aceptada. Un viaje puede tener más de una guía (p.ej. una incompleta
    # y luego la correcta) -- se listan todas, la más nueva primero.
    existing_waybills = query_all(
        "SELECT id, series, series_number, sunat_status, sunat_pdf_url, shipper_name FROM waybills WHERE trip_id = ? ORDER BY id DESC",
        (trip_id,),
    )
    # 22 sep, pedido de Braulio ("que usuario creo el viaje... etc"): quién
    # y cuándo se creó, según activity_log (ver app/audit.py) -- None para
    # viajes de antes de que existiera este registro.
    creator = get_creator_info("viaje", trip_id)
    # 29 sep, pedido de Braulio (ida/vuelta): si este viaje ES una vuelta,
    # `outbound_trip` es su viaje de ida; si este viaje ES una ida,
    # `return_trip` es su vuelta ya creada (o None si todavía no se creó
    # -- ver el botón "Crear viaje de vuelta" en viajes/detail.html). La
    # conformidad de entrega es opcional únicamente en la vuelta de Harraso
    # (en BRMS sigue siendo obligatoria, igual que en cualquier ida) -- ver
    # change_status().
    outbound_trip = None
    return_trip = None
    if trip["return_of_trip_id"]:
        outbound_trip = query_one(
            "SELECT id, code, origin, destination FROM trips WHERE id = ?", (trip["return_of_trip_id"],)
        )
    else:
        return_trip = _return_trip_of(trip_id)
    conformidad_opcional = bool(trip["return_of_trip_id"]) and trip["issuer"] == "HARRASO"
    # 5 oct: ¿la liquidación de este viaje se comparte con su ida/vuelta?
    # Una vuelta la comparte si su ancla es la ida; una ida la comparte si
    # su vuelta (si ya existe) tiene a esta ida como ancla. Los textos
    # "viaje redondo" de viajes/detail.html dependen de esto, no de que
    # simplemente exista el enlace ida/vuelta.
    if outbound_trip:
        liquidacion_compartida = liquidacion_trip_id == outbound_trip["id"]
    elif return_trip:
        liquidacion_compartida = liquidation_anchor_trip_id(return_trip["id"]) == trip["id"]
    else:
        liquidacion_compartida = False
    return render_template(
        "viajes/detail.html", trip=trip, expenses=expenses,
        total_expenses=total_expenses, next_statuses=next_statuses, advance=advance, advance2=advance2,
        anchor_driver_name=anchor_trip_info["driver_name"], anchor_driver2_name=anchor_trip_info["driver2_name"],
        payment_term_labels=payment_term_labels,
        existing_waybills=existing_waybills, creator=creator,
        outbound_trip=outbound_trip, return_trip=return_trip, conformidad_opcional=conformidad_opcional,
        liquidacion_trip_id=liquidacion_trip_id, liquidacion_compartida=liquidacion_compartida,
        guides=trip_guides(trip_id),
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
        #
        # 29 sep, pedido de Braulio (ida/vuelta): "en el caso de Harraso
        # para la vuelta la conformidad es opcional, en BRMS si es
        # obligatorio" -- única excepción a lo de arriba: un viaje que ES
        # una vuelta (return_of_trip_id) de Harraso puede marcarse
        # Entregado sin el archivo (ver el botón correspondiente en
        # viajes/detail.html, que manda este mismo POST directo). Cualquier
        # otro caso -- ida de cualquier empresa, o vuelta de BRMS -- sigue
        # exigiendo el archivo igual que siempre.
        conformidad_opcional = bool(trip["return_of_trip_id"]) and trip["issuer"] == "HARRASO"
        if not trip["delivery_proof_filename"] and not conformidad_opcional:
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

    log_activity(
        "viajes", "ESTADO", f"Viaje {trip['code']}: {trip['status']} → {new_status}",
        entity_type="viaje", entity_id=trip_id,
        entity_url=url_for("viajes.detail", trip_id=trip_id),
    )
    flash(f"Viaje marcado como {new_status.replace('_', ' ').title()}.", "success")
    return redirect(url_for("viajes.detail", trip_id=trip_id))


def _trip_delete_block_reason(trip_id):
    """29 sep, pedido de Braulio ("que solo el administrador pueda borrar
    viajes", confirmando que sí se puede borrar en cascada todo lo
    relacionado): la única razón para NO poder borrar un viaje es que ya
    tenga una factura o guía ACEPTADA por SUNAT -- esos comprobantes
    electrónicos ya quedaron emitidos así, no pueden desaparecer del ERP
    sin dejar un documento "fantasma" allá (mismo criterio que
    _invoice_delete_block_reason() en app/routes/facturacion.py y
    _waybill_delete_block_reason() en app/routes/guias.py). Cualquier otra
    cosa asociada (inspecciones, liquidación, guía NO enviada, ítems de
    factura NO aceptada) sí se borra en cascada junto con el viaje -- ver
    delete_trip().

    29 sep, pedido de Braulio (ida/vuelta): tampoco se puede borrar un
    viaje de ida que todavía tiene un viaje de vuelta enlazado -- ese
    viaje de vuelta es un registro aparte, con su propia factura/GPS/etc.
    (ver return_of_trip_id en schema.sql), y no está contemplado en el
    borrado en cascada de acá; hay que borrar (o cancelar) primero la
    vuelta."""
    linked_return = _return_trip_of(trip_id)
    if linked_return:
        return (
            f"Este viaje de ida tiene un viaje de vuelta enlazado ({linked_return['code']}) — bórralo (o "
            "cancélalo) primero antes de borrar este viaje."
        )
    invoice = query_one(
        """SELECT i.number FROM invoices i JOIN invoice_items ii ON ii.invoice_id = i.id
           WHERE ii.trip_id = ? AND i.sunat_status = 'ACEPTADO' LIMIT 1""",
        (trip_id,),
    )
    if invoice:
        return (
            f"Este viaje está facturado en la factura {invoice['number']}, ya aceptada por SUNAT — "
            "no se puede borrar. Usa \"Cancelar\" en su lugar."
        )
    waybill = query_one(
        "SELECT series, series_number FROM waybills WHERE trip_id = ? AND sunat_status = 'ACEPTADO' LIMIT 1",
        (trip_id,),
    )
    if waybill:
        return (
            f"Este viaje tiene la guía {waybill['series']}-{waybill['series_number']:06d}, ya aceptada "
            "por SUNAT — no se puede borrar. Usa \"Cancelar\" en su lugar."
        )
    return None


@bp.route("/<int:trip_id>/eliminar", methods=["POST"])
@permission_required("viajes", "delete")
def delete_trip(trip_id):
    """Acción "delete" propia, que por defecto solo tiene Administrador (ver
    PERMISSIONS en app/auth.py y el comentario en app/permissions_catalog.py).
    Borra el viaje y, en cascada, todo lo que depende únicamente de él
    (inspecciones, liquidación con su combustible/pagos, gastos, guía de
    remisión e ítems de factura) -- ver _trip_delete_block_reason() arriba
    para la única excepción (documento ya aceptado por SUNAT)."""
    if not validate_csrf():
        abort(400)
    trip = query_one("SELECT * FROM trips WHERE id = ?", (trip_id,))
    if trip is None:
        abort(404)
    reason = _trip_delete_block_reason(trip_id)
    if reason:
        flash(reason, "error")
        return redirect(url_for("viajes.detail", trip_id=trip_id))

    # Inspecciones del viaje.
    execute(
        "DELETE FROM inspection_items WHERE inspection_id IN (SELECT id FROM inspections WHERE trip_id = ?)",
        (trip_id,),
    )
    execute("DELETE FROM inspections WHERE trip_id = ?", (trip_id,))

    # Liquidación del viaje (anticipos con su combustible/pagos) y gastos.
    execute(
        "DELETE FROM fuel_entries WHERE advance_id IN (SELECT id FROM expense_advances WHERE trip_id = ?)",
        (trip_id,),
    )
    execute(
        "DELETE FROM advance_payments WHERE advance_id IN (SELECT id FROM expense_advances WHERE trip_id = ?)",
        (trip_id,),
    )
    execute("DELETE FROM expenses WHERE trip_id = ?", (trip_id,))
    execute("DELETE FROM expense_advances WHERE trip_id = ?", (trip_id,))

    # Guía de remisión del viaje (ya se confirmó arriba que no está
    # aceptada por SUNAT).
    execute("DELETE FROM waybills WHERE trip_id = ?", (trip_id,))
    execute("DELETE FROM trip_waybill_files WHERE trip_id = ?", (trip_id,))

    # Ítems de factura de este viaje -- ya se confirmó arriba que ninguna
    # de esas facturas está aceptada por SUNAT. Si a alguna no le queda
    # ningún ítem después de esto, se borra entera (una factura no puede
    # quedar sin ítems -- mismo criterio que editar factura, ver edit() en
    # app/routes/facturacion.py); si le quedan otros ítems (factura con
    # varios viajes), se recalcula el total.
    affected_invoice_ids = [
        r["invoice_id"] for r in query_all(
            "SELECT DISTINCT invoice_id FROM invoice_items WHERE trip_id = ?", (trip_id,)
        )
    ]
    execute("DELETE FROM invoice_items WHERE trip_id = ?", (trip_id,))
    for invoice_id in affected_invoice_ids:
        remaining = query_all("SELECT amount FROM invoice_items WHERE invoice_id = ?", (invoice_id,))
        if remaining:
            new_total = sum(r["amount"] for r in remaining)
            execute("UPDATE invoices SET amount = ? WHERE id = ?", (new_total, invoice_id))
        else:
            execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))

    execute("DELETE FROM trips WHERE id = ?", (trip_id,))
    log_activity(
        "viajes", "ELIMINAR",
        f"Viaje {trip['code']}: eliminado junto con todo lo relacionado (inspecciones, liquidación, "
        "guía e ítems de factura no aceptados por SUNAT)",
        entity_type="viaje", entity_id=trip_id,
    )
    flash("Viaje eliminado, junto con todo lo relacionado.", "success")
    return redirect(url_for("viajes.list_view"))


# --- Guía de remisión del cliente (3 sep, pedido de Braulio) --------------
#
# Documento que emitió el cliente/remitente del viaje (los nombres de
# columnas/funciones siguen con el prefijo "carrier_waybill_*"/"guía de
# transportista" de cuando se creó este campo, 3 sep -- ver la corrección de
# Braulio del 1 oct junto al panel en viajes/detail.html: el nombre VISIBLE
# en pantalla estaba al revés, esto NO es la guía de transportista -- esa es
# la guía de remisión electrónica real que ya genera el módulo Guías, ver
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


# --- Varias guías por viaje, sin reemplazarse (5 oct, pedido de Braulio) --
#
# "hay que tener la opcion de subir guia de remitente y tambien la guia de
# transportista nuestra (cuando es generada en otro portal)... una vez
# subidas ya no debe figurar la opcion de subir archivo, a menos que sea
# para agregar. No quiero que al subir uno nuevo reemplace al anterior."
# Cada guía subida es una fila de trip_waybill_files (ver schema.sql); subir
# otra SIEMPRE agrega una fila nueva, nunca pisa el archivo anterior. Las
# columnas viejas de trips (shipper_waybill_* / carrier_waybill_*) solo se
# llenan con la primera guía de cada tipo, para app/routes/guias.py.
GUIDE_KINDS = {
    "REMITENTE": {
        "save": lambda fs: _save_shipper_waybill_file(fs),
        "legacy_number": "shipper_waybill_number",
        "legacy_filename": "shipper_waybill_filename",
        "label": "Guía del remitente",
    },
    "TRANSPORTISTA": {
        "save": lambda fs: _save_waybill_file(fs),
        "legacy_number": "carrier_waybill_number",
        "legacy_filename": "carrier_waybill_filename",
        "label": "Guía de transportista",
    },
}


def add_trip_guide(trip_id, kind, number, file_storage):
    """Agrega UNA guía (número y/o archivo) a un viaje, sin tocar las que ya
    tiene. Devuelve True si se guardó algo, False si no venía ni número ni
    un archivo válido."""
    cfg = GUIDE_KINDS[kind]
    number = (number or "").strip() or None
    filename = cfg["save"](file_storage)
    if not number and not filename:
        return False
    user = getattr(g, "user", None)
    execute(
        "INSERT INTO trip_waybill_files (trip_id, kind, guide_number, filename, created_by) VALUES (?, ?, ?, ?, ?)",
        (trip_id, kind, number, filename, user["id"] if user else None),
    )
    # Columnas viejas de trips: solo la primera guía de cada tipo (si ya
    # hay una, no se pisa).
    legacy = query_one(
        f"SELECT {cfg['legacy_number']} AS n, {cfg['legacy_filename']} AS f FROM trips WHERE id = ?", (trip_id,)
    )
    if legacy is not None:
        if number and not legacy["n"]:
            execute(f"UPDATE trips SET {cfg['legacy_number']} = ? WHERE id = ?", (number, trip_id))
        if filename and not legacy["f"]:
            execute(f"UPDATE trips SET {cfg['legacy_filename']} = ? WHERE id = ?", (filename, trip_id))
    return True


def trip_guides(trip_id):
    """{kind: [guías]} de un viaje, la más vieja primero."""
    rows = query_all(
        """SELECT f.id, f.kind, f.guide_number, f.filename, f.created_at, u.name AS created_by_name
           FROM trip_waybill_files f LEFT JOIN users u ON u.id = f.created_by
           WHERE f.trip_id = ? ORDER BY f.id""",
        (trip_id,),
    )
    result = {"REMITENTE": [], "TRANSPORTISTA": []}
    for row in rows:
        result.setdefault(row["kind"], []).append(row)
    return result


@bp.route("/<int:trip_id>/guias/<int:file_id>/archivo")
@permission_required("viajes", "view")
def trip_guide_file(trip_id, file_id):
    row = query_one(
        "SELECT kind, filename FROM trip_waybill_files WHERE id = ? AND trip_id = ?", (file_id, trip_id)
    )
    if row is None or not row["filename"]:
        abort(404)
    if row["kind"] == "REMITENTE":
        if storage.using_s3():
            return redirect(storage.shipper_waybill_url(row["filename"]))
        return send_from_directory(storage.local_shipper_waybills_dir(), row["filename"])
    if storage.using_s3():
        return redirect(storage.carrier_waybill_url(row["filename"]))
    return send_from_directory(storage.local_carrier_waybills_dir(), row["filename"])


@bp.route("/<int:trip_id>/guias/<int:file_id>/eliminar", methods=["POST"])
@permission_required("viajes", "edit")
def delete_trip_guide(trip_id, file_id):
    """5 oct, pedido de Braulio ("sumar ese boton para borrar guias por
    error"): quita UNA guía adjunta (la fila de trip_waybill_files); las
    demás no se tocan. Las columnas viejas de trips se vuelven a calcular
    con la primera guía que queda de ese tipo (o NULL) -- si no, el
    respaldo de arranque (_backfill_trip_waybill_files_*) volvería a crear
    la guía borrada a partir de ellas. El archivo en sí no se borra del
    almacenamiento (la app nunca borra adjuntos, igual que al eliminar un
    viaje)."""
    if not validate_csrf():
        abort(400)
    row = query_one(
        """SELECT f.id, f.kind, f.guide_number, t.code FROM trip_waybill_files f
           JOIN trips t ON t.id = f.trip_id WHERE f.id = ? AND f.trip_id = ?""",
        (file_id, trip_id),
    )
    if row is None:
        abort(404)
    execute("DELETE FROM trip_waybill_files WHERE id = ?", (file_id,))
    cfg = GUIDE_KINDS[row["kind"]]
    remaining = query_all(
        "SELECT guide_number, filename FROM trip_waybill_files WHERE trip_id = ? AND kind = ? ORDER BY id",
        (trip_id, row["kind"]),
    )
    first_number = next((r["guide_number"] for r in remaining if r["guide_number"]), None)
    first_filename = next((r["filename"] for r in remaining if r["filename"]), None)
    execute(
        f"UPDATE trips SET {cfg['legacy_number']} = ?, {cfg['legacy_filename']} = ? WHERE id = ?",
        (first_number, first_filename, trip_id),
    )
    log_activity(
        "viajes", "ELIMINAR",
        f"{cfg['label']} {row['guide_number'] or '(sin número)'} del viaje {row['code']}: eliminada",
        entity_type="viaje", entity_id=trip_id,
        entity_url=url_for("viajes.detail", trip_id=trip_id),
    )
    flash(f"{cfg['label']} eliminada.", "success")
    return redirect(url_for("viajes.detail", trip_id=trip_id))


@bp.route("/<int:trip_id>/guia", methods=["POST"])
@permission_required("viajes", "edit")
def save_waybill(trip_id):
    """Guía de transportista (nuestra, emitida en otro portal): AGREGA una
    guía al viaje; las anteriores se conservan (ver add_trip_guide())."""
    if not validate_csrf():
        abort(400)
    trip = query_one("SELECT code FROM trips WHERE id = ?", (trip_id,))
    if trip is None:
        abort(404)
    added = add_trip_guide(
        trip_id, "TRANSPORTISTA", request.form.get("carrier_waybill_number"), request.files.get("carrier_waybill_file")
    )
    if not added:
        flash("Escribe el número de la guía o adjunta un archivo (foto o PDF).", "error")
        return redirect(url_for("viajes.detail", trip_id=trip_id))
    log_activity(
        "viajes", "SUBIR",
        f"Guía de transportista (nuestra) del viaje {trip['code']}",
        entity_type="viaje", entity_id=trip_id,
        entity_url=url_for("viajes.detail", trip_id=trip_id),
    )
    flash("Guía de transportista agregada.", "success")
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


# --- Guía de remisión del remitente (15 sep, pedido de Braulio) -----------
#
# "Una vez iniciado el viaje, a la hora de crear o subir guia primero debe
# especificarse si la guia de remision figura nuestros datos como
# transportista. Si figuran, no es necesario emitir una guia nueva, solo
# adjuntar la de remitente. Si no figuran, ahi es necesario crear la guia de
# transportista." Documento distinto de "Guía de remisión del cliente" de
# arriba (carrier_waybill_*, ver la corrección de nombre del 1 oct) -- ver
# el comentario en schema.sql junto a
# shipper_waybill_shows_carrier. Solo aplica a viajes con ownership !=
# 'TERCERO' (si el viaje lo hizo un tercero subcontratado, el transportista
# de la guía del remitente sería ese tercero, no Harraso/BRMS -- la
# pregunta no tiene sentido, ver viajes/detail.html).


@bp.route("/<int:trip_id>/guia-remitente/decision", methods=["POST"])
@permission_required("viajes", "edit")
def set_shipper_waybill_decision(trip_id):
    if not validate_csrf():
        abort(400)
    trip = query_one("SELECT code FROM trips WHERE id = ?", (trip_id,))
    if trip is None:
        abort(404)
    decision = request.form.get("decision", "").strip().upper()
    if decision not in ("SI", "NO", ""):
        abort(400)
    execute(
        "UPDATE trips SET shipper_waybill_shows_carrier=? WHERE id=?",
        (decision or None, trip_id),
    )
    log_activity(
        "viajes", "EDITAR",
        f"Viaje {trip['code']}: guía del remitente muestra a Harraso/BRMS = {decision or 'sin responder'}",
        entity_type="viaje", entity_id=trip_id,
        entity_url=url_for("viajes.detail", trip_id=trip_id),
    )
    if decision == "SI":
        flash("Guía del remitente: ya figura con nuestros datos como transportista.", "success")
    elif decision == "NO":
        flash("Guía del remitente: no figura con nuestros datos -- genera la guía de transportista.", "success")
    else:
        flash("Respuesta reiniciada.", "success")
    return redirect(url_for("viajes.detail", trip_id=trip_id))


def _save_shipper_waybill_file(file_storage):
    return _save_binary_attachment(file_storage, storage.save_shipper_waybill)


@bp.route("/<int:trip_id>/guia-remitente", methods=["POST"])
@permission_required("viajes", "edit")
def save_shipper_waybill(trip_id):
    """Guía de remisión del remitente: AGREGA una guía al viaje; las
    anteriores se conservan (ver add_trip_guide())."""
    if not validate_csrf():
        abort(400)
    trip = query_one("SELECT code FROM trips WHERE id = ?", (trip_id,))
    if trip is None:
        abort(404)
    added = add_trip_guide(
        trip_id, "REMITENTE", request.form.get("shipper_waybill_number"), request.files.get("shipper_waybill_file")
    )
    if not added:
        flash("Escribe el número de la guía o adjunta un archivo (foto o PDF).", "error")
        return redirect(url_for("viajes.detail", trip_id=trip_id))
    log_activity(
        "viajes", "SUBIR",
        f"Guía del remitente del viaje {trip['code']}",
        entity_type="viaje", entity_id=trip_id,
        entity_url=url_for("viajes.detail", trip_id=trip_id),
    )
    flash("Guía del remitente agregada.", "success")
    return redirect(url_for("viajes.detail", trip_id=trip_id))


@bp.route("/<int:trip_id>/guia-remitente/archivo")
@permission_required("viajes", "view")
def shipper_waybill_file(trip_id):
    trip = query_one("SELECT shipper_waybill_filename FROM trips WHERE id = ?", (trip_id,))
    if trip is None or not trip["shipper_waybill_filename"]:
        abort(404)
    if storage.using_s3():
        return redirect(storage.shipper_waybill_url(trip["shipper_waybill_filename"]))
    return send_from_directory(storage.local_shipper_waybills_dir(), trip["shipper_waybill_filename"])


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
    trip = query_one("SELECT code, status, delivery_proof_filename FROM trips WHERE id = ?", (trip_id,))
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
    log_activity(
        "viajes", "SUBIR", f"Conformidad de entrega del viaje {trip['code']} — marcado como Entregado",
        entity_type="viaje", entity_id=trip_id,
        entity_url=url_for("viajes.detail", trip_id=trip_id),
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


# --- Foto de evidencia del contenedor (10 sep, pedido de Braulio) ---------
#
# "cuando se eliga tipo de carga contenedor, tiene que haber la opcion de
# registrar el codigo del contenedor y asimismo se pueda subir una foto de
# evidencia de que el contenedor esta en buen estado." A diferencia de la
# guía de transportista / conformidad de entrega, se registra directo en el
# formulario de alta/edición del viaje (viajes/form.html), no aparte — y
# siempre es una foto (nunca un PDF), así que no reusa
# _save_binary_attachment() (que sí acepta PDF).

def _save_container_photo_file(file_storage):
    """Guarda la foto de evidencia del estado del contenedor y devuelve el
    nombre guardado, o None si no se subió nada válido. Mismo patrón que
    _save_binary_attachment(), pero restringido a fotos."""
    if not file_storage or not file_storage.filename:
        return None
    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in ALLOWED_CONTAINER_PHOTO_EXTENSIONS:
        ext = CONTAINER_PHOTO_MIME_TO_EXTENSION.get((file_storage.mimetype or "").lower())
    if not ext:
        return None
    raw_bytes = file_storage.read()
    if not raw_bytes:
        return None
    compressed = compress_photo(raw_bytes)
    if compressed is not None:
        filename = f"{uuid.uuid4().hex}.jpg"
        storage.save_container_photo(filename, compressed)
        return filename
    filename = f"{uuid.uuid4().hex}{ext}"
    storage.save_container_photo(filename, raw_bytes)
    return filename


@bp.route("/<int:trip_id>/contenedor/foto")
@permission_required("viajes", "view")
def container_photo_file(trip_id):
    trip = query_one("SELECT container_photo_filename FROM trips WHERE id = ?", (trip_id,))
    if trip is None or not trip["container_photo_filename"]:
        abort(404)
    if storage.using_s3():
        return redirect(storage.container_photo_url(trip["container_photo_filename"]))
    return send_from_directory(storage.local_container_photos_dir(), trip["container_photo_filename"])


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
    trip = query_one("SELECT code, invoiced FROM trips WHERE id = ?", (trip_id,))
    if trip is None:
        abort(404)
    new_value = 0 if trip["invoiced"] else 1
    execute("UPDATE trips SET invoiced=? WHERE id=?", (new_value, trip_id))
    log_activity(
        "viajes", "FACTURAR" if new_value else "EDITAR",
        f"Viaje {trip['code']} {'marcado' if new_value else 'desmarcado'} como facturado",
        entity_type="viaje", entity_id=trip_id,
        entity_url=url_for("viajes.detail", trip_id=trip_id),
    )
    flash("Viaje marcado como facturado." if new_value else "Viaje desmarcado como facturado.", "success")
    return redirect(url_for("viajes.detail", trip_id=trip_id))


@bp.route("/<int:trip_id>/pagado", methods=["POST"])
@_billing_permission_required
def toggle_paid(trip_id):
    if not validate_csrf():
        abort(400)
    trip = query_one("SELECT code, paid FROM trips WHERE id = ?", (trip_id,))
    if trip is None:
        abort(404)
    new_value = 0 if trip["paid"] else 1
    execute("UPDATE trips SET paid=? WHERE id=?", (new_value, trip_id))
    log_activity(
        "viajes", "PAGAR" if new_value else "EDITAR",
        f"Viaje {trip['code']} {'marcado' if new_value else 'desmarcado'} como pagado",
        entity_type="viaje", entity_id=trip_id,
        entity_url=url_for("viajes.detail", trip_id=trip_id),
    )
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
           GROUP BY driver_id, driver_name, origin, destination
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
