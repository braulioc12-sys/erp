from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app.auth import permission_required, validate_csrf
from app.db import execute, query_all, query_one

bp = Blueprint("clientes", __name__, url_prefix="/clientes")


@bp.route("")
@permission_required("clientes", "view")
def list_view():
    q = request.args.get("q", "").strip()
    if q:
        clients = query_all(
            "SELECT * FROM clients WHERE active = 1 AND (name LIKE ? OR ruc LIKE ?) ORDER BY name",
            (f"%{q}%", f"%{q}%"),
        )
    else:
        clients = query_all("SELECT * FROM clients WHERE active = 1 ORDER BY name")
    return render_template("clientes/list.html", clients=clients, q=q)


@bp.route("/nuevo", methods=["GET", "POST"])
@permission_required("clientes", "edit")
def new():
    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        name = request.form.get("name", "").strip()
        if not name:
            flash("El nombre del cliente es obligatorio.", "error")
            return render_template("clientes/form.html", client=request.form, mode="new")
        execute(
            "INSERT INTO clients (name, ruc, phone, email, address) VALUES (?, ?, ?, ?, ?)",
            (
                name,
                request.form.get("ruc", "").strip(),
                request.form.get("phone", "").strip(),
                request.form.get("email", "").strip(),
                request.form.get("address", "").strip(),
            ),
        )
        flash("Cliente creado correctamente.", "success")
        return redirect(url_for("clientes.list_view"))
    return render_template("clientes/form.html", client=None, mode="new")


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
            return render_template("clientes/form.html", client=request.form, mode="edit", client_id=client_id)
        execute(
            "UPDATE clients SET name=?, ruc=?, phone=?, email=?, address=? WHERE id=?",
            (
                name,
                request.form.get("ruc", "").strip(),
                request.form.get("phone", "").strip(),
                request.form.get("email", "").strip(),
                request.form.get("address", "").strip(),
                client_id,
            ),
        )
        flash("Cliente actualizado.", "success")
        return redirect(url_for("clientes.list_view"))
    return render_template("clientes/form.html", client=client, mode="edit", client_id=client_id)


@bp.route("/<int:client_id>/eliminar", methods=["POST"])
@permission_required("clientes", "edit")
def delete(client_id):
    if not validate_csrf():
        abort(400)
    in_use = query_one("SELECT COUNT(*) n FROM trips WHERE client_id = ?", (client_id,))["n"]
    if in_use:
        execute("UPDATE clients SET active = 0 WHERE id = ?", (client_id,))
        flash("El cliente tiene viajes asociados; se marcó como inactivo.", "success")
    else:
        execute("DELETE FROM clients WHERE id = ?", (client_id,))
        flash("Cliente eliminado.", "success")
    return redirect(url_for("clientes.list_view"))
