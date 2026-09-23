"""Catálogos editables por el administrador: conceptos de mantenimiento,
tipos de gasto, etc. — para no tener que tocar código cada vez que se
necesita agregar una opción nueva a un desplegable."""
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for

from app.alerts import alert_recipient_emails, build_alert_sections, total_alert_count
from app.audit import log_activity
from app.auth import permission_required, validate_csrf
from app.db import execute, get_setting, query_all, query_one, set_setting
from app.email_sender import send_email
from app.helpers import get_detraction_concepts, parse_float, today_str
from app.seed_data import MECHANIC_TYPES, labor_cost_setting_key

bp = Blueprint("catalogos", __name__, url_prefix="/configuracion/catalogos")

CATEGORIES = {
    "maintenance_type": "Conceptos de mantenimiento",
    "inspection_item": "Ítems de inspección",
    "vehicle_owner": "Propietarios de unidades",
}


def get_catalog(category, only_active=True):
    sql = "SELECT * FROM catalog_items WHERE category = ?"
    params = [category]
    if only_active:
        sql += " AND active = 1"
    sql += " ORDER BY sort_order, name"
    return query_all(sql, params)


@bp.route("")
@permission_required("catalogos", "view")
def list_view():
    active_category = request.args.get("categoria", "maintenance_type")
    if active_category not in CATEGORIES:
        active_category = "maintenance_type"
    items = query_all(
        "SELECT * FROM catalog_items WHERE category = ? ORDER BY sort_order, name",
        (active_category,),
    )
    labor_costs = {t: get_setting(labor_cost_setting_key(t), "0") for t in MECHANIC_TYPES}
    return render_template(
        "catalogos/list.html", categories=CATEGORIES, active_category=active_category, items=items,
        labor_costs=labor_costs, mechanic_types=MECHANIC_TYPES,
    )


@bp.route("/costo-mano-obra", methods=["POST"])
@permission_required("catalogos", "edit")
def update_labor_cost():
    if not validate_csrf():
        abort(400)
    values = {}
    for t in MECHANIC_TYPES:
        value = parse_float(request.form.get(labor_cost_setting_key(t)), None)
        if value is None or value < 0:
            flash(f"Indica un costo de mano de obra por minuto válido para {t}.", "error")
            return redirect(url_for("catalogos.list_view"))
        values[t] = value
    for t, value in values.items():
        set_setting(labor_cost_setting_key(t), f"{value:.2f}")
    # 22 sep, registro de actividad (ver app/audit.py): esto edita settings
    # (ver get_setting/set_setting en app/db.py), no una fila de catálogo con
    # id propio -- se registra sin entity_id, solo con el detalle de los
    # valores nuevos.
    log_activity(
        "catalogos", "EDITAR",
        "Costos de mano de obra: " + ", ".join(f"{t} S/{v:.2f}" for t, v in values.items()),
        entity_type="costo_mano_obra",
    )
    flash("Costos de mano de obra actualizados.", "success")
    return redirect(url_for("catalogos.list_view"))


@bp.route("/agregar", methods=["POST"])
@permission_required("catalogos", "edit")
def add_item():
    if not validate_csrf():
        abort(400)
    category = request.form.get("category")
    name = request.form.get("name", "").strip()
    if category not in CATEGORIES:
        abort(400)
    if not name:
        flash("Escribe un nombre para el nuevo concepto.", "error")
        return redirect(url_for("catalogos.list_view", categoria=category))

    existing = query_one(
        "SELECT id, active FROM catalog_items WHERE category = ? AND name = ?", (category, name)
    )
    if existing:
        if existing["active"]:
            flash("Ese concepto ya existe.", "error")
        else:
            execute("UPDATE catalog_items SET active = 1 WHERE id = ?", (existing["id"],))
            # 22 sep, registro de actividad (ver app/audit.py): entity_type
            # distingue la categoría (varias comparten esta misma vista
            # genérica, ver CATEGORIES arriba) para poder filtrar en Actividad.
            log_activity(
                "catalogos", "REACTIVAR", f"{CATEGORIES[category]}: {name}",
                entity_type=f"catalogo_{category}", entity_id=existing["id"],
            )
            flash(f'"{name}" reactivado.', "success")
    else:
        max_order = query_one(
            "SELECT COALESCE(MAX(sort_order), -1) m FROM catalog_items WHERE category = ?", (category,)
        )["m"]
        item_id = execute(
            "INSERT INTO catalog_items (category, name, sort_order) VALUES (?, ?, ?)",
            (category, name, max_order + 1),
        )
        log_activity(
            "catalogos", "CREAR", f"{CATEGORIES[category]}: {name}",
            entity_type=f"catalogo_{category}", entity_id=item_id,
        )
        flash(f'"{name}" agregado.', "success")
    return redirect(url_for("catalogos.list_view", categoria=category))


@bp.route("/<int:item_id>/alternar", methods=["POST"])
@permission_required("catalogos", "edit")
def toggle_item(item_id):
    if not validate_csrf():
        abort(400)
    item = query_one("SELECT * FROM catalog_items WHERE id = ?", (item_id,))
    if item is None:
        abort(404)
    execute("UPDATE catalog_items SET active = ? WHERE id = ?", (0 if item["active"] else 1, item_id))
    # 22 sep, registro de actividad (ver app/audit.py).
    log_activity(
        "catalogos", "DESACTIVAR" if item["active"] else "REACTIVAR",
        f"{CATEGORIES.get(item['category'], item['category'])}: {item['name']}",
        entity_type=f"catalogo_{item['category']}", entity_id=item_id,
    )
    flash("Actualizado." if item["active"] else "Reactivado.", "success")
    return redirect(url_for("catalogos.list_view", categoria=item["category"]))


# --- Grifos (10 sep, 3ra ronda, pedido de Braulio: "dentro de catalogos
# hay que poner los grifos, en este se registren de acuerdo a su ciudad
# razon social y ruc, en la pantalla de liquidaciones se eligan los que
# estan registrados"). Catálogo propio (no el genérico catalog_items de
# arriba, que solo maneja un nombre) porque un grifo necesita 3 datos —
# ver CREATE TABLE fuel_stations en schema.sql. Se usa desde el formulario
# de gastos y desde el panel de combustible de la liquidación (ver
# app/routes/liquidaciones.py). ---

@bp.route("/grifos")
@permission_required("catalogos", "view")
def grifos_list():
    stations = query_all("SELECT * FROM fuel_stations ORDER BY city, business_name")
    return render_template("catalogos/grifos.html", stations=stations)


@bp.route("/grifos/agregar", methods=["POST"])
@permission_required("catalogos", "edit")
def grifos_add():
    if not validate_csrf():
        abort(400)
    city = request.form.get("city", "").strip()
    business_name = request.form.get("business_name", "").strip()
    ruc = request.form.get("ruc", "").strip()
    if not city or not business_name or not ruc:
        flash("Completa ciudad, razón social y RUC.", "error")
        return redirect(url_for("catalogos.grifos_list"))

    max_order = query_one("SELECT COALESCE(MAX(sort_order), -1) m FROM fuel_stations")["m"]
    station_id = execute(
        "INSERT INTO fuel_stations (city, business_name, ruc, sort_order) VALUES (?, ?, ?, ?)",
        (city, business_name, ruc, max_order + 1),
    )
    # 22 sep, registro de actividad (ver app/audit.py).
    log_activity(
        "catalogos", "CREAR", f"Grifo: {business_name} ({city})",
        entity_type="grifo", entity_id=station_id,
    )
    flash(f'Grifo "{business_name}" ({city}) agregado.', "success")
    return redirect(url_for("catalogos.grifos_list"))


@bp.route("/grifos/<int:station_id>/alternar", methods=["POST"])
@permission_required("catalogos", "edit")
def grifos_toggle(station_id):
    if not validate_csrf():
        abort(400)
    station = query_one("SELECT * FROM fuel_stations WHERE id = ?", (station_id,))
    if station is None:
        abort(404)
    execute("UPDATE fuel_stations SET active = ? WHERE id = ?", (0 if station["active"] else 1, station_id))
    # 22 sep, registro de actividad (ver app/audit.py).
    log_activity(
        "catalogos", "DESACTIVAR" if station["active"] else "REACTIVAR",
        f"Grifo: {station['business_name']} ({station['city']})",
        entity_type="grifo", entity_id=station_id,
    )
    flash("Actualizado." if station["active"] else "Reactivado.", "success")
    return redirect(url_for("catalogos.grifos_list"))


# --- Bancos (18 sep, 2da ronda, pedido de Braulio al armar el archivo real
# de Telecrédito: "Hay que crear archivos por empresa, Harraso y BRMS
# tienen cuentas distintas. En el menu de catalogos pon la parte de bancos
# en la cual yo pueda registrar las cuentas de cada empresa y estas se
# seleccionen a la hora de crear el archivo y se llenen sus datos.") --
# cuentas de cargo (la cuenta desde la que se paga) por empresa del grupo,
# usadas al generar el archivo de Telecrédito en Pagos personal (ver
# app/routes/pagos_personal.py telecredito_configure/telecredito_generate
# y app/telecredito.py). Mismo patrón que Grifos arriba (tabla propia,
# activar/desactivar en vez de borrar). ---

BANK_ACCOUNT_TYPE_LABELS = {"CORRIENTE": "Cuenta Corriente", "MAESTRA": "Cuenta Maestra"}
BANK_CURRENCY_LABELS = {"S": "Soles", "D": "Dólares"}


@bp.route("/bancos")
@permission_required("catalogos", "view")
def bancos_list():
    accounts = query_all("SELECT * FROM company_bank_accounts ORDER BY company_name, sort_order")
    return render_template(
        "catalogos/bancos.html", accounts=accounts,
        account_type_labels=BANK_ACCOUNT_TYPE_LABELS, currency_labels=BANK_CURRENCY_LABELS,
    )


@bp.route("/bancos/agregar", methods=["POST"])
@permission_required("catalogos", "edit")
def bancos_add():
    if not validate_csrf():
        abort(400)
    company_name = request.form.get("company_name", "").strip()
    bank_name = request.form.get("bank_name", "").strip() or "BCP"
    account_type = request.form.get("account_type", "")
    currency = request.form.get("currency", "")
    account_number = "".join(ch for ch in request.form.get("account_number", "") if ch.isdigit())
    alias = request.form.get("alias", "").strip() or None

    errors = []
    if not company_name:
        errors.append("Indica la empresa dueña de la cuenta.")
    if account_type not in BANK_ACCOUNT_TYPE_LABELS:
        errors.append("Elige el tipo de cuenta (Corriente o Maestra) — Telecrédito no admite Ahorros como cuenta de cargo.")
    if currency not in BANK_CURRENCY_LABELS:
        errors.append("Elige la moneda de la cuenta.")
    if not account_number:
        errors.append("Indica el número de cuenta (solo dígitos).")
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("catalogos.bancos_list"))

    max_order = query_one("SELECT COALESCE(MAX(sort_order), -1) m FROM company_bank_accounts")["m"]
    account_id = execute(
        """INSERT INTO company_bank_accounts
           (company_name, bank_name, account_type, currency, account_number, alias, sort_order)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (company_name, bank_name, account_type, currency, account_number, alias, max_order + 1),
    )
    # 22 sep, registro de actividad (ver app/audit.py).
    log_activity(
        "catalogos", "CREAR", f"Cuenta bancaria de {company_name}: {bank_name} ({account_number})",
        entity_type="banco", entity_id=account_id,
    )
    flash(f'Cuenta de "{company_name}" agregada.', "success")
    return redirect(url_for("catalogos.bancos_list"))


@bp.route("/bancos/<int:account_id>/alternar", methods=["POST"])
@permission_required("catalogos", "edit")
def bancos_toggle(account_id):
    if not validate_csrf():
        abort(400)
    account = query_one("SELECT * FROM company_bank_accounts WHERE id = ?", (account_id,))
    if account is None:
        abort(404)
    execute("UPDATE company_bank_accounts SET active = ? WHERE id = ?", (0 if account["active"] else 1, account_id))
    # 22 sep, registro de actividad (ver app/audit.py).
    log_activity(
        "catalogos", "DESACTIVAR" if account["active"] else "REACTIVAR",
        f"Cuenta bancaria de {account['company_name']}: {account['bank_name']} ({account['account_number']})",
        entity_type="banco", entity_id=account_id,
    )
    flash("Actualizado." if account["active"] else "Reactivado.", "success")
    return redirect(url_for("catalogos.bancos_list"))


# --- Conceptos de detracción (22 sep, 2da ronda, pedido de Braulio: "en
# la parte de catalogos hay que incluir conceptos de detraccion y en este
# se puedan agregar o modificar los conceptos o porcentajes de los que ya
# tienes registrados para las facturas") -- catálogo propio (código,
# nombre, porcentaje), igual que Grifos/Bancos arriba, pero con un extra:
# a diferencia de esos dos (que solo se activan/desactivan), acá también
# se puede EDITAR un concepto ya existente -- Braulio pidió explícitamente
# poder "modificar los conceptos o porcentajes", no solo darlos de baja.
# Usado por Facturación para el selector de bienes al confirmar una
# detracción (ver get_detraction_goods_catalog() en app/helpers.py) y para
# el porcentaje del cálculo automático del código 027 en facturas 100%
# viajes (ver get_detraction_percentage() en app/helpers.py). Se siembra
# una sola vez con el contenido que antes vivía fijo en código -- ver
# _seed_detraction_concepts_sqlite/_postgres en app/db.py. ---


@bp.route("/detraccion")
@permission_required("catalogos", "view")
def detraccion_list():
    concepts = get_detraction_concepts(only_active=False)
    return render_template("catalogos/detraccion.html", concepts=concepts)


@bp.route("/detraccion/agregar", methods=["POST"])
@permission_required("catalogos", "edit")
def detraccion_add():
    if not validate_csrf():
        abort(400)
    code = request.form.get("code", "").strip()
    name = request.form.get("name", "").strip()
    percentage = parse_float(request.form.get("percentage"), None)
    # 23 sep, pedido de Braulio: código propio de tefacturo.pe para el
    # campo "codigoBienServicio" del bloque "detraccion" de la factura (ver
    # el comentario largo en schema.sql) -- opcional, se puede dejar en
    # blanco hasta que tefacturo.pe lo confirme para este concepto.
    tefacturo_code = request.form.get("tefacturo_codigo_bien_servicio", "").strip() or None

    errors = []
    if not code:
        errors.append("Indica el código SUNAT del bien/servicio.")
    if not name:
        errors.append("Indica el nombre del concepto.")
    if percentage is None or percentage < 0 or percentage > 100:
        errors.append("Indica un porcentaje válido (entre 0 y 100).")
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("catalogos.detraccion_list"))

    existing = query_one("SELECT id, active FROM detraction_concepts WHERE code = ?", (code,))
    if existing:
        if existing["active"]:
            flash(f'Ya existe un concepto con el código "{code}" — edítalo abajo en vez de agregarlo de nuevo.', "error")
        else:
            execute(
                "UPDATE detraction_concepts SET active = 1, name = ?, percentage = ?, "
                "tefacturo_codigo_bien_servicio = ? WHERE id = ?",
                (name, percentage, tefacturo_code, existing["id"]),
            )
            # 22 sep, registro de actividad (ver app/audit.py).
            log_activity(
                "catalogos", "REACTIVAR", f"Concepto de detracción: {code} — {name} ({percentage}%)",
                entity_type="concepto_detraccion", entity_id=existing["id"],
            )
            flash(f'"{code}" reactivado.', "success")
    else:
        max_order = query_one("SELECT COALESCE(MAX(sort_order), -1) m FROM detraction_concepts")["m"]
        concept_id = execute(
            "INSERT INTO detraction_concepts (code, name, percentage, sort_order, tefacturo_codigo_bien_servicio) "
            "VALUES (?, ?, ?, ?, ?)",
            (code, name, percentage, max_order + 1, tefacturo_code),
        )
        log_activity(
            "catalogos", "CREAR", f"Concepto de detracción: {code} — {name} ({percentage}%)",
            entity_type="concepto_detraccion", entity_id=concept_id,
        )
        flash(f'Concepto de detracción "{code} — {name}" agregado.', "success")
    return redirect(url_for("catalogos.detraccion_list"))


@bp.route("/detraccion/<int:concept_id>/editar", methods=["POST"])
@permission_required("catalogos", "edit")
def detraccion_edit(concept_id):
    if not validate_csrf():
        abort(400)
    concept = query_one("SELECT * FROM detraction_concepts WHERE id = ?", (concept_id,))
    if concept is None:
        abort(404)
    code = request.form.get("code", "").strip()
    name = request.form.get("name", "").strip()
    percentage = parse_float(request.form.get("percentage"), None)
    tefacturo_code = request.form.get("tefacturo_codigo_bien_servicio", "").strip() or None

    errors = []
    if not code:
        errors.append("Indica el código SUNAT del bien/servicio.")
    if not name:
        errors.append("Indica el nombre del concepto.")
    if percentage is None or percentage < 0 or percentage > 100:
        errors.append("Indica un porcentaje válido (entre 0 y 100).")
    if code != concept["code"]:
        clash = query_one("SELECT id FROM detraction_concepts WHERE code = ? AND id != ?", (code, concept_id))
        if clash:
            errors.append(f'Ya hay otro concepto con el código "{code}".')
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("catalogos.detraccion_list"))

    execute(
        "UPDATE detraction_concepts SET code = ?, name = ?, percentage = ?, "
        "tefacturo_codigo_bien_servicio = ? WHERE id = ?",
        (code, name, percentage, tefacturo_code, concept_id),
    )
    # 22 sep, registro de actividad (ver app/audit.py).
    log_activity(
        "catalogos", "EDITAR", f"Concepto de detracción: {code} — {name} ({percentage}%)",
        entity_type="concepto_detraccion", entity_id=concept_id,
    )
    flash(f'Concepto "{code} — {name}" actualizado.', "success")
    return redirect(url_for("catalogos.detraccion_list"))


@bp.route("/detraccion/<int:concept_id>/alternar", methods=["POST"])
@permission_required("catalogos", "edit")
def detraccion_toggle(concept_id):
    if not validate_csrf():
        abort(400)
    concept = query_one("SELECT * FROM detraction_concepts WHERE id = ?", (concept_id,))
    if concept is None:
        abort(404)
    execute("UPDATE detraction_concepts SET active = ? WHERE id = ?", (0 if concept["active"] else 1, concept_id))
    # 22 sep, registro de actividad (ver app/audit.py).
    log_activity(
        "catalogos", "DESACTIVAR" if concept["active"] else "REACTIVAR",
        f"Concepto de detracción: {concept['code']} — {concept['name']}",
        entity_type="concepto_detraccion", entity_id=concept_id,
    )
    flash("Actualizado." if concept["active"] else "Reactivado.", "success")
    return redirect(url_for("catalogos.detraccion_list"))


# 20 sep, pedido de Braulio ("como podemos hacer para que se envien
# alertas automaticas a los correos?"): esta pantalla es solo para ver si
# AWS SES está configurado y probar el envío a mano -- el envío
# automático de verdad corre aparte, por fuera de un request HTTP normal,
# desde un Cron Job de Render que ejecuta send_alerts.py (ver el
# comentario largo en ese archivo y en app/email_sender.py/config.py).


@bp.route("/alertas-correo")
@permission_required("catalogos", "view")
def alertas_correo():
    sections = build_alert_sections()
    return render_template(
        "catalogos/alertas_correo.html",
        sections=sections,
        total=total_alert_count(sections),
        ses_sender=(current_app.config.get("SES_SENDER_EMAIL") or "").strip(),
        recipients=alert_recipient_emails(),
    )


@bp.route("/alertas-correo/enviar", methods=["POST"])
@permission_required("catalogos", "edit")
def alertas_correo_enviar():
    if not validate_csrf():
        abort(400)
    # 21 sep, pedido de Braulio: el destinatario ya no es un correo fijo --
    # es el correo de login de cada usuario activo (ver
    # alert_recipient_emails() en app/alerts.py).
    to = alert_recipient_emails()
    if not to:
        flash(
            "No hay ningún destinatario -- no hay usuarios activos con correo, ni ALERT_EMAIL_TO configurado.",
            "error",
        )
        return redirect(url_for("catalogos.alertas_correo"))

    periodo = request.form.get("periodo", "diario")
    if periodo not in ("diario", "semanal"):
        periodo = "diario"
    sections = build_alert_sections()
    total = total_alert_count(sections)
    etiqueta = "Resumen diario" if periodo == "diario" else "Resumen semanal"
    subject = (
        f"Harris — {etiqueta} de alertas ({total}) [PRUEBA]"
        if total
        else f"Harris — {etiqueta}: sin alertas pendientes [PRUEBA]"
    )
    html = render_template(
        "email/alertas.html", sections=sections, total=total, periodo=periodo, today=today_str()
    )
    ok, error = send_email(to, subject, html)
    if ok:
        # 22 sep, registro de actividad (ver app/audit.py): no hay una fila
        # propia que identifique este envío (es un correo de prueba, no una
        # entidad guardada) -- se registra sin entity_type/entity_id, solo
        # para que quede quién lo mandó y a quiénes.
        log_activity(
            "catalogos", "ENVIAR", f"Correo de prueba de alertas ({etiqueta.lower()}) a {', '.join(to)}",
        )
        flash(f"Correo de prueba enviado a {', '.join(to)} ({total} alerta(s)).", "success")
    else:
        flash(f"No se pudo enviar el correo: {error}", "error")
    return redirect(url_for("catalogos.alertas_correo"))
