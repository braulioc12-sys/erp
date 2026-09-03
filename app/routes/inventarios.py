"""Módulo Inventarios: catálogo de repuestos con stock, proveedores, y
compras (orden de compra, proveedor, cantidad, precio) — pedido de
Braulio, 29 ago: "cada compra de repuestos debe figurar el proveedor,
orden de compra, cantidad y precio. Una vez que se ingrese al stock
disponible de nuestro almacén el área de mantenimiento puede disponer de
estos repuestos."

Unifica lo que antes era el catálogo "Materiales" de Mantenimiento (ver
app/routes/mantenimiento.py) — ahora esos mismos repuestos viven aquí, con
stock real: las compras (una vez marcadas "Recibida") suman al stock, y
usarlos en una orden de mantenimiento resta (permitiendo stock negativo,
solo con aviso — pedido explícito de Braulio, no se bloquea)."""
from datetime import datetime

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from app.auth import permission_required, validate_csrf
from app.db import execute, get_db, query_all, query_one
from app.helpers import parse_date, parse_float, today_str

bp = Blueprint("inventarios", __name__, url_prefix="/inventarios")


# --- Catálogo de repuestos (antes "Materiales" en Mantenimiento) ---

def get_catalog_items(only_active=True):
    sql = "SELECT * FROM inventory_items WHERE 1=1"
    if only_active:
        sql += " AND active = 1"
    sql += " ORDER BY sort_order, name"
    return query_all(sql)


@bp.route("")
@permission_required("inventarios", "view")
def list_view():
    q = request.args.get("q", "").strip()
    sql = "SELECT * FROM inventory_items WHERE 1=1"
    params = []
    if q:
        sql += " AND name LIKE ?"
        params.append(f"%{q}%")
    sql += " ORDER BY sort_order, name"
    items = query_all(sql, params)
    pending_purchases = query_one(
        "SELECT COUNT(*) n FROM inventory_purchases WHERE status = 'PENDIENTE'"
    )["n"]
    return render_template(
        "inventarios/list.html", items=items, pending_purchases=pending_purchases, q=q
    )


@bp.route("/repuestos/<int:item_id>")
@permission_required("inventarios", "view")
def item_detail(item_id):
    """Detalle de un repuesto: sus datos y su historial de compras — a qué
    proveedor, cuándo, cuánta cantidad y a qué precio — pedido de Braulio,
    29 ago: "poder buscar los repuestos y que salga su historial de
    compras que se hizo a cada proveedor y a qué precio"."""
    item = query_one("SELECT * FROM inventory_items WHERE id = ?", (item_id,))
    if item is None:
        abort(404)
    purchase_history = query_all(
        """SELECT pi.quantity, pi.unit_price, pi.received_quantity, p.id AS purchase_id,
                  p.provider_name, p.purchase_order_number, p.purchase_date, p.status,
                  p.authorized_at
           FROM inventory_purchase_items pi
           JOIN inventory_purchases p ON p.id = pi.purchase_id
           WHERE pi.item_id = ?
           ORDER BY p.purchase_date DESC, p.id DESC""",
        (item_id,),
    )
    # Precio último/promedio a partir de lo REALMENTE recibido por línea
    # (received_quantity), no del estado de la orden completa — desde que
    # existen recepciones parciales (1 sep) una línea puede tener mercadería
    # recibida aunque el resto de la orden siga en camino, y antes esto se
    # quedaba invisible hasta que TODA la orden se marcara recibida.
    received = [h for h in purchase_history if (h["received_quantity"] or 0) > 0]
    last_price = received[0]["unit_price"] if received else None
    total_qty = sum(h["received_quantity"] or 0 for h in received)
    total_amount = sum((h["received_quantity"] or 0) * (h["unit_price"] or 0) for h in received)
    avg_price = (total_amount / total_qty) if total_qty else None
    return render_template(
        "inventarios/item_detail.html",
        item=item,
        purchase_history=purchase_history,
        last_price=last_price,
        avg_price=avg_price,
    )


@bp.route("/repuestos/agregar", methods=["POST"])
@permission_required("inventarios", "edit")
def items_add():
    if not validate_csrf():
        abort(400)
    name = request.form.get("name", "").strip()
    unit_cost = parse_float(request.form.get("unit_cost"), 0)
    stock_quantity = parse_float(request.form.get("stock_quantity"), 0) or 0
    if not name:
        flash("Escribe el nombre del repuesto.", "error")
        return redirect(url_for("inventarios.list_view"))

    existing = query_one("SELECT id, active FROM inventory_items WHERE name = ?", (name,))
    if existing:
        if existing["active"]:
            flash("Ese repuesto ya existe.", "error")
        else:
            execute(
                "UPDATE inventory_items SET active = 1, unit_cost = ? WHERE id = ?",
                (unit_cost, existing["id"]),
            )
            flash(f'"{name}" reactivado.', "success")
    else:
        max_order = query_one("SELECT COALESCE(MAX(sort_order), -1) m FROM inventory_items")["m"]
        execute(
            "INSERT INTO inventory_items (name, unit_cost, stock_quantity, sort_order) VALUES (?, ?, ?, ?)",
            (name, unit_cost, stock_quantity, max_order + 1),
        )
        flash(f'"{name}" agregado.', "success")
    return redirect(url_for("inventarios.list_view"))


@bp.route("/repuestos/<int:item_id>/alternar", methods=["POST"])
@permission_required("inventarios", "edit")
def items_toggle(item_id):
    if not validate_csrf():
        abort(400)
    item = query_one("SELECT * FROM inventory_items WHERE id = ?", (item_id,))
    if item is None:
        abort(404)
    execute("UPDATE inventory_items SET active = ? WHERE id = ?", (0 if item["active"] else 1, item_id))
    flash("Actualizado." if item["active"] else "Reactivado.", "success")
    return redirect(url_for("inventarios.list_view"))


@bp.route("/repuestos/<int:item_id>/ajustar-stock", methods=["POST"])
@permission_required("inventarios", "edit")
def items_adjust_stock(item_id):
    """Ajuste manual de stock (ej. para corregir un conteo físico, o para
    cuadrar el stock inicial al cargar el inventario real) — pone el stock
    directamente en el valor indicado, no lo suma."""
    if not validate_csrf():
        abort(400)
    item = query_one("SELECT * FROM inventory_items WHERE id = ?", (item_id,))
    if item is None:
        abort(404)
    new_stock = parse_float(request.form.get("stock_quantity"), None)
    if new_stock is None:
        flash("Indica una cantidad de stock válida.", "error")
        return redirect(url_for("inventarios.list_view"))
    execute("UPDATE inventory_items SET stock_quantity = ? WHERE id = ?", (new_stock, item_id))
    flash(f'Stock de "{item["name"]}" ajustado a {new_stock}.', "success")
    return redirect(url_for("inventarios.list_view"))


# --- Proveedores ---

def get_catalog_providers(only_active=True):
    sql = "SELECT * FROM inventory_providers WHERE 1=1"
    if only_active:
        sql += " AND active = 1"
    sql += " ORDER BY sort_order, name"
    return query_all(sql)


@bp.route("/proveedores")
@permission_required("inventarios", "view")
def providers_list():
    providers = query_all("SELECT * FROM inventory_providers ORDER BY sort_order, name")
    return render_template("inventarios/providers.html", providers=providers)


@bp.route("/proveedores/agregar", methods=["POST"])
@permission_required("inventarios", "edit")
def providers_add():
    if not validate_csrf():
        abort(400)
    name = request.form.get("name", "").strip()
    ruc = request.form.get("ruc", "").strip() or None
    phone = request.form.get("phone", "").strip() or None
    if not name:
        flash("Escribe el nombre del proveedor.", "error")
        return redirect(url_for("inventarios.providers_list"))

    existing = query_one("SELECT id, active FROM inventory_providers WHERE name = ?", (name,))
    if existing:
        if existing["active"]:
            flash("Ese proveedor ya existe.", "error")
        else:
            execute(
                "UPDATE inventory_providers SET active = 1, ruc = ?, phone = ? WHERE id = ?",
                (ruc, phone, existing["id"]),
            )
            flash(f'"{name}" reactivado.', "success")
    else:
        max_order = query_one("SELECT COALESCE(MAX(sort_order), -1) m FROM inventory_providers")["m"]
        execute(
            "INSERT INTO inventory_providers (name, ruc, phone, sort_order) VALUES (?, ?, ?, ?)",
            (name, ruc, phone, max_order + 1),
        )
        flash(f'"{name}" agregado.', "success")
    return redirect(url_for("inventarios.providers_list"))


@bp.route("/proveedores/<int:provider_id>/alternar", methods=["POST"])
@permission_required("inventarios", "edit")
def providers_toggle(provider_id):
    if not validate_csrf():
        abort(400)
    provider = query_one("SELECT * FROM inventory_providers WHERE id = ?", (provider_id,))
    if provider is None:
        abort(404)
    execute(
        "UPDATE inventory_providers SET active = ? WHERE id = ?", (0 if provider["active"] else 1, provider_id)
    )
    flash("Actualizado." if provider["active"] else "Reactivado.", "success")
    return redirect(url_for("inventarios.providers_list"))


# --- Compras (proveedor + orden de compra + líneas de cantidad/precio) ---
#
# Flujo (1 sep, pedido de Braulio): PENDIENTE (borrador, editable/eliminable)
# -> un Administrador la AUTORIZA (queda registrado quién y cuándo, ya no se
# puede editar/eliminar) -> recién ahí se puede generar el PDF para el
# proveedor y confirmar la recepción de los repuestos -> la recepción puede
# llegar en varias entregas parciales (no siempre llega todo junto) hasta
# que se completa sola o el usuario la cierra manualmente con lo que llegó.
# La columna `status` de la tabla sigue siendo solo PENDIENTE/RECIBIDO (no
# se le agregó un tercer valor para no tener que alterar el CHECK ya
# desplegado en producción) — el estado real que se muestra en pantalla se
# calcula comparando `authorized_at` y las cantidades recibidas por línea,
# ver _purchase_display_status().

def _purchase_progress(items):
    """(total pedido, total recibido hasta ahora, si ya se recibió TODO) a
    partir de las líneas de una orden."""
    total_qty = sum(i["quantity"] or 0 for i in items)
    received_qty = sum(i["received_quantity"] or 0 for i in items)
    all_received = bool(items) and all(
        (i["received_quantity"] or 0) >= (i["quantity"] or 0) for i in items
    )
    return total_qty, received_qty, all_received


def _purchase_display_status(purchase, items):
    """Estado mostrado en pantalla (código, etiqueta) — no es la columna
    `status` de la base, que solo distingue PENDIENTE/RECIBIDO."""
    if purchase["status"] == "RECIBIDO":
        _, _, all_received = _purchase_progress(items)
        if all_received:
            return "RECIBIDO", "Recibida"
        return "RECIBIDO", "Cerrada (con faltantes)"
    if not purchase["authorized_at"]:
        return "PENDIENTE", "Pendiente de autorización"
    _, received_qty, _ = _purchase_progress(items)
    if received_qty > 0:
        return "PARCIAL", "Autorizada — recepción parcial"
    return "AUTORIZADA", "Autorizada — esperando recepción"


def _purchases_with_totals(status=None):
    sql = "SELECT * FROM inventory_purchases WHERE 1=1"
    params = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY purchase_date DESC, id DESC"
    purchases = query_all(sql, params)
    if not purchases:
        return []
    ids = [p["id"] for p in purchases]
    placeholders = ",".join("?" * len(ids))
    items = query_all(
        f"SELECT * FROM inventory_purchase_items WHERE purchase_id IN ({placeholders})", ids
    )
    totals = {}
    items_by_purchase = {}
    for it in items:
        totals[it["purchase_id"]] = totals.get(it["purchase_id"], 0) + (it["quantity"] or 0) * (it["unit_price"] or 0)
        items_by_purchase.setdefault(it["purchase_id"], []).append(it)
    result = []
    for p in purchases:
        row = dict(p)
        row["total"] = totals.get(p["id"], 0)
        code, label = _purchase_display_status(p, items_by_purchase.get(p["id"], []))
        row["display_status_code"] = code
        row["display_status_label"] = label
        result.append(row)
    return result


@bp.route("/compras")
@permission_required("inventarios", "view")
def purchases_list():
    purchases = _purchases_with_totals()
    return render_template("inventarios/purchases_list.html", purchases=purchases)


@bp.route("/compras/nueva", methods=["GET", "POST"])
@permission_required("inventarios", "edit")
def purchases_new():
    providers = get_catalog_providers()
    items = get_catalog_items()

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        provider_id = request.form.get("provider_id")
        provider = query_one("SELECT * FROM inventory_providers WHERE id = ?", (provider_id,)) if provider_id else None
        purchase_order_number = request.form.get("purchase_order_number", "").strip()
        purchase_date = parse_date(request.form.get("purchase_date")) or today_str()
        item_ids = [int(i) for i in request.form.getlist("item_ids")]
        selected_items = [i for i in items if i["id"] in item_ids]

        errors = []
        if provider is None:
            errors.append("Selecciona un proveedor.")
        if not selected_items:
            errors.append("Marca al menos un repuesto con su cantidad y precio.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "inventarios/purchase_form.html", providers=providers, items=items, purchase=request.form,
            )

        purchase_id = execute(
            """INSERT INTO inventory_purchases
               (provider_id, provider_name, purchase_order_number, purchase_date, status, notes)
               VALUES (?, ?, ?, ?, 'PENDIENTE', ?)""",
            (provider["id"], provider["name"], purchase_order_number or None, purchase_date,
             request.form.get("notes", "").strip()),
        )
        db = get_db()
        any_line = False
        for i in selected_items:
            qty = parse_float(request.form.get(f"item_qty_{i['id']}"), 0) or 0
            price = parse_float(request.form.get(f"item_price_{i['id']}"), 0) or 0
            if qty <= 0:
                continue
            any_line = True
            db.execute(
                """INSERT INTO inventory_purchase_items (purchase_id, item_id, item_name, quantity, unit_price)
                   VALUES (?, ?, ?, ?, ?)""",
                (purchase_id, i["id"], i["name"], qty, price),
            )
        db.commit()
        if not any_line:
            # Ningún repuesto marcado terminó con cantidad > 0 — no dejamos
            # una orden de compra vacía.
            execute("DELETE FROM inventory_purchases WHERE id = ?", (purchase_id,))
            flash("Indica una cantidad mayor a 0 en al menos un repuesto.", "error")
            return render_template(
                "inventarios/purchase_form.html", providers=providers, items=items, purchase=request.form,
            )

        flash("Orden de compra registrada como pendiente de autorización.", "success")
        return redirect(url_for("inventarios.purchases_detail", purchase_id=purchase_id))

    return render_template(
        "inventarios/purchase_form.html", providers=providers, items=items, purchase=None, today=today_str(),
    )


@bp.route("/compras/<int:purchase_id>")
@permission_required("inventarios", "view")
def purchases_detail(purchase_id):
    purchase = query_one("SELECT * FROM inventory_purchases WHERE id = ?", (purchase_id,))
    if purchase is None:
        abort(404)
    items = query_all(
        "SELECT * FROM inventory_purchase_items WHERE purchase_id = ? ORDER BY id", (purchase_id,)
    )
    total = sum((i["quantity"] or 0) * (i["unit_price"] or 0) for i in items)
    total_qty, received_qty, all_received = _purchase_progress(items)
    display_status_code, display_status_label = _purchase_display_status(purchase, items)
    receptions = query_all(
        "SELECT * FROM inventory_purchase_receptions WHERE purchase_id = ? ORDER BY id DESC",
        (purchase_id,),
    )
    reception_items = {}
    if receptions:
        ids = [r["id"] for r in receptions]
        placeholders = ",".join("?" * len(ids))
        rows = query_all(
            f"SELECT * FROM inventory_purchase_reception_items WHERE reception_id IN ({placeholders}) ORDER BY id",
            ids,
        )
        for row in rows:
            reception_items.setdefault(row["reception_id"], []).append(row)
    return render_template(
        "inventarios/purchase_detail.html",
        purchase=purchase, items=items, total=total,
        total_qty=total_qty, received_qty=received_qty, all_received=all_received,
        display_status_code=display_status_code, display_status_label=display_status_label,
        receptions=receptions, reception_items=reception_items,
        is_admin=("ADMIN" in g.user["roles"]),
    )


@bp.route("/compras/<int:purchase_id>/autorizar", methods=["POST"])
@permission_required("inventarios", "edit")
def purchases_authorize(purchase_id):
    """Autoriza la orden de compra — exclusivo de Administrador (pedido
    explícito de Braulio, 1 sep: "cuando lo autoriza un administrador se
    registra su autorización debajo de la orden"), chequeado por ROL
    directamente aquí y no solo por el permiso "edit" del módulo, ya que
    ese permiso ahora también lo tiene Operador (puede crear la orden y
    confirmar la recepción, pero no autorizar). Una vez autorizada, la
    orden queda bloqueada: ya no se puede editar ni eliminar, recién ahí se
    puede generar su PDF y empezar a registrar recepciones."""
    if not validate_csrf():
        abort(400)
    if "ADMIN" not in g.user["roles"]:
        flash("Solo un Administrador puede autorizar una orden de compra.", "error")
        return redirect(url_for("inventarios.purchases_detail", purchase_id=purchase_id))
    purchase = query_one("SELECT * FROM inventory_purchases WHERE id = ?", (purchase_id,))
    if purchase is None:
        abort(404)
    if purchase["authorized_at"]:
        flash("Esta orden ya estaba autorizada.", "error")
        return redirect(url_for("inventarios.purchases_detail", purchase_id=purchase_id))
    execute(
        """UPDATE inventory_purchases
           SET authorized_at = ?, authorized_by_name = ?, authorized_by_user_id = ?
           WHERE id = ?""",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), g.user["name"], g.user["id"], purchase_id),
    )
    flash(f'Orden de compra autorizada por {g.user["name"]}. Ya se puede generar el PDF y recibir los repuestos.', "success")
    return redirect(url_for("inventarios.purchases_detail", purchase_id=purchase_id))


@bp.route("/compras/<int:purchase_id>/pdf")
@permission_required("inventarios", "view")
def purchases_pdf(purchase_id):
    """Página de impresión de la orden de compra (mismo patrón que
    Inspecciones: página independiente pensada para "Guardar como PDF"
    desde el propio diálogo de impresión del navegador, sin librería de
    PDF en el servidor) — para enviarla al proveedor. Solo disponible una
    vez autorizada, porque el PDF muestra el bloque de autorización
    (quién y cuándo) debajo de la orden, pedido explícito de Braulio."""
    purchase = query_one("SELECT * FROM inventory_purchases WHERE id = ?", (purchase_id,))
    if purchase is None:
        abort(404)
    if not purchase["authorized_at"]:
        flash("Esta orden todavía no está autorizada — no se puede generar el PDF.", "error")
        return redirect(url_for("inventarios.purchases_detail", purchase_id=purchase_id))
    items = query_all(
        "SELECT * FROM inventory_purchase_items WHERE purchase_id = ? ORDER BY id", (purchase_id,)
    )
    total = sum((i["quantity"] or 0) * (i["unit_price"] or 0) for i in items)
    provider = query_one("SELECT * FROM inventory_providers WHERE id = ?", (purchase["provider_id"],)) if purchase["provider_id"] else None
    generated_at = datetime.now().strftime("%d/%m/%Y %H:%M")
    return render_template(
        "inventarios/purchase_pdf.html",
        purchase=purchase, items=items, total=total, provider=provider, generated_at=generated_at,
    )


@bp.route("/compras/<int:purchase_id>/recibir", methods=["GET", "POST"])
@permission_required("inventarios", "edit")
def purchases_receive(purchase_id):
    """Registra una recepción (puede ser parcial — pedido explícito de
    Braulio: "puede que a veces no lleguen todos"): un evento con lo que
    llegó AHORA de cada línea, sumado a lo ya recibido antes. Sube el stock
    de cada repuesto en la cantidad de ESTE evento (no del total de la
    línea) y actualiza su costo de referencia al precio pagado en esta
    orden. Cuando con esto ya se completó el 100% de todas las líneas, la
    orden se cierra sola (status='RECIBIDO')."""
    purchase = query_one("SELECT * FROM inventory_purchases WHERE id = ?", (purchase_id,))
    if purchase is None:
        abort(404)
    if not purchase["authorized_at"]:
        flash("Esta orden todavía no está autorizada — no se puede recibir.", "error")
        return redirect(url_for("inventarios.purchases_detail", purchase_id=purchase_id))
    if purchase["status"] == "RECIBIDO":
        flash("Esta orden ya está cerrada.", "error")
        return redirect(url_for("inventarios.purchases_detail", purchase_id=purchase_id))

    items = query_all(
        "SELECT * FROM inventory_purchase_items WHERE purchase_id = ? ORDER BY id", (purchase_id,)
    )
    pending_items = [i for i in items if (i["received_quantity"] or 0) < (i["quantity"] or 0)]

    if request.method == "POST":
        if not validate_csrf():
            abort(400)

        # Primera pasada: solo lee y valida, sin escribir nada — así una
        # línea inválida no deja una recepción a medias ya escrita (mismo
        # criterio que la validación de rotación de llantas: se valida el
        # conjunto completo antes de tocar la base de datos).
        to_receive = []
        error = None
        for it in pending_items:
            qty_now = parse_float(request.form.get(f"recv_qty_{it['id']}"), 0) or 0
            if qty_now <= 0:
                continue
            pending = (it["quantity"] or 0) - (it["received_quantity"] or 0)
            if qty_now > pending + 1e-9:
                error = f'"{it["item_name"]}": no puede llegar más de lo pendiente ({pending}).'
                break
            to_receive.append((it, qty_now))

        if error:
            flash(error, "error")
            return redirect(url_for("inventarios.purchases_receive", purchase_id=purchase_id))
        if not to_receive:
            flash("Indica cuánto llegó de al menos un repuesto.", "error")
            return redirect(url_for("inventarios.purchases_receive", purchase_id=purchase_id))

        # Segunda pasada: ya validado todo, recién ahí se escribe.
        db = get_db()
        reception_id = db.execute(
            """INSERT INTO inventory_purchase_receptions
               (purchase_id, received_at, received_by_name, received_by_user_id, notes)
               VALUES (?, ?, ?, ?, ?)""",
            (purchase_id, today_str(), g.user["name"], g.user["id"],
             request.form.get("notes", "").strip() or None),
        ).lastrowid
        for it, qty_now in to_receive:
            db.execute(
                """INSERT INTO inventory_purchase_reception_items
                   (reception_id, purchase_item_id, item_name, quantity)
                   VALUES (?, ?, ?, ?)""",
                (reception_id, it["id"], it["item_name"], qty_now),
            )
            db.execute(
                "UPDATE inventory_purchase_items SET received_quantity = received_quantity + ? WHERE id = ?",
                (qty_now, it["id"]),
            )
            if it["item_id"] is not None:
                db.execute(
                    "UPDATE inventory_items SET stock_quantity = stock_quantity + ?, unit_cost = ? WHERE id = ?",
                    (qty_now, it["unit_price"], it["item_id"]),
                )

        updated_items = query_all(
            "SELECT * FROM inventory_purchase_items WHERE purchase_id = ?", (purchase_id,)
        )
        _, _, all_received = _purchase_progress(updated_items)
        if all_received:
            db.execute(
                "UPDATE inventory_purchases SET status = 'RECIBIDO', received_at = ? WHERE id = ?",
                (today_str(), purchase_id),
            )
        db.commit()
        flash(
            "Recepción registrada — se sumó al stock de cada repuesto."
            + (" La orden quedó completa y se cerró." if all_received else " Quedan repuestos pendientes de llegar."),
            "success",
        )
        return redirect(url_for("inventarios.purchases_detail", purchase_id=purchase_id))

    return render_template(
        "inventarios/purchase_receive_form.html", purchase=purchase, items=pending_items,
    )


@bp.route("/compras/<int:purchase_id>/cerrar", methods=["POST"])
@permission_required("inventarios", "edit")
def purchases_close(purchase_id):
    """Cierra la orden aunque no haya llegado todo lo pedido — pedido
    explícito de Braulio ("puede que a veces no lleguen todos"): sin esto,
    una orden con un faltante permanente se quedaría en "recepción
    parcial" para siempre. No cambia lo ya recibido, solo marca la orden
    como cerrada."""
    if not validate_csrf():
        abort(400)
    purchase = query_one("SELECT * FROM inventory_purchases WHERE id = ?", (purchase_id,))
    if purchase is None:
        abort(404)
    if not purchase["authorized_at"]:
        flash("Esta orden todavía no está autorizada.", "error")
        return redirect(url_for("inventarios.purchases_detail", purchase_id=purchase_id))
    if purchase["status"] == "RECIBIDO":
        flash("Esta orden ya estaba cerrada.", "error")
        return redirect(url_for("inventarios.purchases_detail", purchase_id=purchase_id))
    execute(
        "UPDATE inventory_purchases SET status = 'RECIBIDO', received_at = ? WHERE id = ?",
        (today_str(), purchase_id),
    )
    flash("Orden cerrada con lo recibido hasta ahora.", "success")
    return redirect(url_for("inventarios.purchases_detail", purchase_id=purchase_id))


@bp.route("/compras/<int:purchase_id>/eliminar", methods=["POST"])
@permission_required("inventarios", "edit")
def purchases_delete(purchase_id):
    """Solo se puede eliminar una compra todavía sin autorizar — una vez
    autorizada representa un compromiso real (se le manda el PDF al
    proveedor), y borrarla dejaría el historial inconsistente con lo que
    en verdad pasó."""
    if not validate_csrf():
        abort(400)
    purchase = query_one("SELECT * FROM inventory_purchases WHERE id = ?", (purchase_id,))
    if purchase is None:
        abort(404)
    if purchase["authorized_at"]:
        flash("No se puede eliminar una orden ya autorizada.", "error")
        return redirect(url_for("inventarios.purchases_detail", purchase_id=purchase_id))
    execute("DELETE FROM inventory_purchase_items WHERE purchase_id = ?", (purchase_id,))
    execute("DELETE FROM inventory_purchases WHERE id = ?", (purchase_id,))
    flash("Orden de compra eliminada.", "success")
    return redirect(url_for("inventarios.purchases_list"))
