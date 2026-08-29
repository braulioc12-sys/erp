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
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

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
        """SELECT pi.quantity, pi.unit_price, p.id AS purchase_id, p.provider_name,
                  p.purchase_order_number, p.purchase_date, p.status
           FROM inventory_purchase_items pi
           JOIN inventory_purchases p ON p.id = pi.purchase_id
           WHERE pi.item_id = ?
           ORDER BY p.purchase_date DESC, p.id DESC""",
        (item_id,),
    )
    received = [h for h in purchase_history if h["status"] == "RECIBIDO"]
    last_price = received[0]["unit_price"] if received else None
    total_qty = sum(h["quantity"] or 0 for h in received)
    total_amount = sum((h["quantity"] or 0) * (h["unit_price"] or 0) for h in received)
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
    for it in items:
        totals[it["purchase_id"]] = totals.get(it["purchase_id"], 0) + (it["quantity"] or 0) * (it["unit_price"] or 0)
    result = []
    for p in purchases:
        row = dict(p)
        row["total"] = totals.get(p["id"], 0)
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

        flash("Orden de compra registrada como pendiente.", "success")
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
    return render_template("inventarios/purchase_detail.html", purchase=purchase, items=items, total=total)


@bp.route("/compras/<int:purchase_id>/recibir", methods=["POST"])
@permission_required("inventarios", "edit")
def purchases_receive(purchase_id):
    """Marca la compra como RECIBIDA: suma la cantidad de cada línea al
    stock del repuesto correspondiente, y actualiza su costo unitario de
    referencia al precio pagado en esta compra (último precio gana —
    mismo criterio de "copia al momento" que ya usa el resto del
    proyecto, aplicado aquí al costo de referencia en vez de al nombre)."""
    if not validate_csrf():
        abort(400)
    purchase = query_one("SELECT * FROM inventory_purchases WHERE id = ?", (purchase_id,))
    if purchase is None:
        abort(404)
    if purchase["status"] == "RECIBIDO":
        flash("Esta compra ya estaba marcada como recibida.", "error")
        return redirect(url_for("inventarios.purchases_detail", purchase_id=purchase_id))

    items = query_all(
        "SELECT * FROM inventory_purchase_items WHERE purchase_id = ?", (purchase_id,)
    )
    db = get_db()
    for it in items:
        if it["item_id"] is None:
            continue
        db.execute(
            "UPDATE inventory_items SET stock_quantity = stock_quantity + ?, unit_cost = ? WHERE id = ?",
            (it["quantity"], it["unit_price"], it["item_id"]),
        )
    db.execute(
        "UPDATE inventory_purchases SET status = 'RECIBIDO', received_at = ? WHERE id = ?",
        (today_str(), purchase_id),
    )
    db.commit()
    flash("Compra marcada como recibida — se sumó al stock de cada repuesto.", "success")
    return redirect(url_for("inventarios.purchases_detail", purchase_id=purchase_id))


@bp.route("/compras/<int:purchase_id>/eliminar", methods=["POST"])
@permission_required("inventarios", "edit")
def purchases_delete(purchase_id):
    """Solo se puede eliminar una compra todavía PENDIENTE — una vez
    recibida ya afectó el stock, y borrarla dejaría el historial
    inconsistente con lo que en verdad pasó con el inventario."""
    if not validate_csrf():
        abort(400)
    purchase = query_one("SELECT * FROM inventory_purchases WHERE id = ?", (purchase_id,))
    if purchase is None:
        abort(404)
    if purchase["status"] == "RECIBIDO":
        flash("No se puede eliminar una compra ya recibida (ya afectó el stock).", "error")
        return redirect(url_for("inventarios.purchases_detail", purchase_id=purchase_id))
    execute("DELETE FROM inventory_purchase_items WHERE purchase_id = ?", (purchase_id,))
    execute("DELETE FROM inventory_purchases WHERE id = ?", (purchase_id,))
    flash("Orden de compra eliminada.", "success")
    return redirect(url_for("inventarios.purchases_list"))
