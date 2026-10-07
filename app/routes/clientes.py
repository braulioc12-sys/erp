from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app.audit import log_activity
from app.auth import permission_required, validate_csrf
from app.db import execute, query_all, query_one

bp = Blueprint("clientes", __name__, url_prefix="/clientes")


# 7 oct, pedido de Braulio ("tengo clientes que la facturacion no es al cliente
# del viaje sino otra razon social... cuando el cliente es Ripley o Honda la
# facturacion es con A&S"): clients.billing_client_id. Los viajes del cliente
# se facturan a ese otro cliente (ver facturacion.new()). Reglas: no puede
# ser el mismo cliente, debe existir y estar activo, y no hay cadenas -- el
# cliente de facturación no puede a su vez facturarse a otro, y un cliente al
# que otros le facturan no puede elegir uno.
def _billing_client_candidates(client_id=None):
    sql = "SELECT id, name FROM clients WHERE active = 1 AND billing_client_id IS NULL"
    params = []
    if client_id:
        sql += " AND id != ?"
        params.append(client_id)
    return query_all(sql + " ORDER BY name", params)


def _billed_through(client_id):
    """Clientes cuyos viajes se facturan a `client_id` (para mostrarlos en su ficha)."""
    if not client_id:
        return []
    return query_all(
        "SELECT id, name FROM clients WHERE billing_client_id = ? AND active = 1 ORDER BY name", (client_id,)
    )


def _billing_client_from_form(client_id=None):
    """Devuelve (billing_client_id | None, mensaje_de_error | None)."""
    raw = (request.form.get("billing_client_id") or "").strip()
    if not raw:
        return None, None
    try:
        target_id = int(raw)
    except ValueError:
        return None, "La razón social de facturación elegida no es válida."
    if client_id and target_id == client_id:
        return None, "Un cliente no puede facturarse a sí mismo — deja el campo vacío si se factura a su propio nombre."
    target = query_one("SELECT id, name, active, billing_client_id FROM clients WHERE id = ?", (target_id,))
    if target is None or not target["active"]:
        return None, "La razón social de facturación elegida no existe o está inactiva."
    if target["billing_client_id"]:
        return None, f"{target['name']} ya se factura a otra razón social — elige directamente la razón social final."
    if client_id and _billed_through(client_id):
        return None, "A este cliente le facturan otros clientes, así que no puede facturarse a otra razón social."
    return target_id, None


@bp.route("")
@permission_required("clientes", "view")
def list_view():
    q = request.args.get("q", "").strip()
    if q:
        # LOWER() en ambos lados (patch 0066): "LIKE" a secas es case-INsensitive en
        # SQLite (donde se prueba en local) pero case-SENSITIVE en Postgres (donde
        # corre producción, ver claude/migracion-aws-rds-s3-patch-0043-notas.md) --
        # con solo "LIKE ?" el buscador funcionaba en pruebas locales pero fallaba en
        # producción para cualquier búsqueda que no calzara la mayúscula/minúscula
        # exacta guardada. Braulio lo reportó con capturas (Catálogo de Personal:
        # "BRAULIO" encontraba, "braulio" no) -- se revisó todo el codebase y el mismo
        # patrón sin LOWER() aparecía en varios buscadores más, corregidos junto con
        # este.
        clients = query_all(
            "SELECT * FROM clients WHERE active = 1 AND (LOWER(name) LIKE LOWER(?) OR LOWER(ruc) LIKE LOWER(?)) ORDER BY name",
            (f"%{q}%", f"%{q}%"),
        )
    else:
        clients = query_all("SELECT * FROM clients WHERE active = 1 ORDER BY name")
    names = {c["id"]: c["name"] for c in query_all("SELECT id, name FROM clients")}
    return render_template("clientes/list.html", clients=clients, q=q, client_names=names)


@bp.route("/nuevo", methods=["GET", "POST"])
@permission_required("clientes", "edit")
def new():
    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        name = request.form.get("name", "").strip()
        if not name:
            flash("El nombre del cliente es obligatorio.", "error")
            return render_template("clientes/form.html", client=request.form, mode="new",
                                   billing_candidates=_billing_client_candidates(), billed_through=[])
        billing_client_id, billing_error = _billing_client_from_form()
        if billing_error:
            flash(billing_error, "error")
            return render_template("clientes/form.html", client=request.form, mode="new",
                                   billing_candidates=_billing_client_candidates(), billed_through=[])
        client_id = execute(
            "INSERT INTO clients (name, ruc, phone, email, address, billing_client_id) VALUES (?, ?, ?, ?, ?, ?)",
            (
                name,
                request.form.get("ruc", "").strip(),
                request.form.get("phone", "").strip(),
                request.form.get("email", "").strip(),
                request.form.get("address", "").strip(),
                billing_client_id,
            ),
        )
        # 22 sep, registro de actividad (ver app/audit.py). Sin pantalla de
        # detalle propia para clientes (solo lista/formulario), el enlace va
        # a editar, la única vista puntual que existe de este registro.
        log_activity(
            "clientes", "CREAR", f"Cliente {name}",
            entity_type="cliente", entity_id=client_id,
            entity_url=url_for("clientes.edit", client_id=client_id),
        )
        flash("Cliente creado correctamente.", "success")
        return redirect(url_for("clientes.list_view"))
    return render_template("clientes/form.html", client=None, mode="new",
                           billing_candidates=_billing_client_candidates(), billed_through=[])


@bp.route("/<int:client_id>/editar", methods=["GET", "POST"])
@permission_required("clientes", "edit")
def edit(client_id):
    client = query_one("SELECT * FROM clients WHERE id = ?", (client_id,))
    if client is None:
        abort(404)
    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        name = request.form.get("name", "").strip()
        if not name:
            flash("El nombre del cliente es obligatorio.", "error")
            return render_template("clientes/form.html", client=request.form, mode="edit", client_id=client_id,
                                   billing_candidates=_billing_client_candidates(client_id),
                                   billed_through=_billed_through(client_id))
        billing_client_id, billing_error = _billing_client_from_form(client_id)
        if billing_error:
            flash(billing_error, "error")
            return render_template("clientes/form.html", client=request.form, mode="edit", client_id=client_id,
                                   billing_candidates=_billing_client_candidates(client_id),
                                   billed_through=_billed_through(client_id))
        execute(
            "UPDATE clients SET name=?, ruc=?, phone=?, email=?, address=?, billing_client_id=? WHERE id=?",
            (
                name,
                request.form.get("ruc", "").strip(),
                request.form.get("phone", "").strip(),
                request.form.get("email", "").strip(),
                request.form.get("address", "").strip(),
                billing_client_id,
                client_id,
            ),
        )
        # 22 sep, registro de actividad (ver app/audit.py).
        log_activity(
            "clientes", "EDITAR", f"Cliente {name}",
            entity_type="cliente", entity_id=client_id,
            entity_url=url_for("clientes.edit", client_id=client_id),
        )
        flash("Cliente actualizado.", "success")
        return redirect(url_for("clientes.list_view"))
    return render_template("clientes/form.html", client=client, mode="edit", client_id=client_id,
                           billing_candidates=_billing_client_candidates(client_id),
                           billed_through=_billed_through(client_id))


@bp.route("/<int:client_id>/eliminar", methods=["POST"])
@permission_required("clientes", "edit")
def delete(client_id):
    if not validate_csrf():
        abort(400)
    client = query_one("SELECT * FROM clients WHERE id = ?", (client_id,))
    if client is None:
        abort(404)
    billed_for = _billed_through(client_id)
    if billed_for:
        flash(
            "No se puede eliminar: los viajes de " + ", ".join(b["name"] for b in billed_for)
            + " se facturan a este cliente. Cámbiales primero la razón social de facturación.",
            "error",
        )
        return redirect(url_for("clientes.list_view"))
    in_use = query_one("SELECT COUNT(*) n FROM trips WHERE client_id = ?", (client_id,))["n"]
    if in_use:
        execute("UPDATE clients SET active = 0 WHERE id = ?", (client_id,))
        # 22 sep, registro de actividad (ver app/audit.py): baja lógica (tiene
        # viajes asociados) -- DESACTIVAR, no ELIMINAR, ya que el registro sigue existiendo.
        log_activity(
            "clientes", "DESACTIVAR", f'Cliente {client["name"]}',
            entity_type="cliente", entity_id=client_id,
        )
        flash("El cliente tiene viajes asociados; se marcó como inactivo.", "success")
    else:
        execute("DELETE FROM clients WHERE id = ?", (client_id,))
        # 22 sep, registro de actividad (ver app/audit.py) -- sin entity_url:
        # el cliente ya no existe, no hay a dónde enlazar.
        log_activity(
            "clientes", "ELIMINAR", f'Cliente {client["name"]}',
            entity_type="cliente", entity_id=client_id,
        )
        flash("Cliente eliminado.", "success")
    return redirect(url_for("clientes.list_view"))
