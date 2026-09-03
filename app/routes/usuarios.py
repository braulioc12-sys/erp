import secrets

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash

from app.auth import ROLE_LABELS, permission_required, validate_csrf
from app.db import USER_ROLES, execute, query_all, query_one

bp = Blueprint("usuarios", __name__, url_prefix="/usuarios")

# Lista (rol, etiqueta) en el mismo orden que USER_ROLES (app/db.py, fuente
# única de qué roles acepta la base de datos), para el desplegable del
# formulario — ver app/auth.py ROLE_LABELS/PERMISSIONS para el detalle de
# qué puede ver/editar cada uno.
ROLE_CHOICES = [(r, ROLE_LABELS.get(r, r)) for r in USER_ROLES]


@bp.route("")
@permission_required("usuarios", "view")
def list_view():
    users = query_all("SELECT * FROM users ORDER BY name")
    return render_template("usuarios/list.html", users=users, role_labels=ROLE_LABELS)


@bp.route("/nuevo", methods=["GET", "POST"])
@permission_required("usuarios", "edit")
def new():
    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        role = request.form.get("role", "OPERADOR")
        password = request.form.get("password", "")

        errors = []
        if not name or not email:
            errors.append("Nombre y correo son obligatorios.")
        if role not in USER_ROLES:
            errors.append("Rol inválido.")
        if len(password) < 6:
            errors.append("La contraseña debe tener al menos 6 caracteres.")
        if email and query_one("SELECT id FROM users WHERE email = ?", (email,)):
            errors.append("Ya existe un usuario con ese correo.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("usuarios/form.html", user=request.form, mode="new", role_choices=ROLE_CHOICES)

        execute(
            "INSERT INTO users (name, email, password_hash, role) VALUES (?, ?, ?, ?)",
            (name, email, generate_password_hash(password), role),
        )
        flash("Usuario creado.", "success")
        return redirect(url_for("usuarios.list_view"))

    return render_template(
        "usuarios/form.html", user=None, mode="new",
        suggested_password=secrets.token_urlsafe(6), role_choices=ROLE_CHOICES,
    )


@bp.route("/<int:user_id>/editar", methods=["GET", "POST"])
@permission_required("usuarios", "edit")
def edit(user_id):
    user = query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    if user is None:
        abort(404)

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        name = request.form.get("name", "").strip()
        role = request.form.get("role", user["role"])
        active = 1 if request.form.get("active") else 0
        new_password = request.form.get("password", "")

        if role not in USER_ROLES:
            flash("Rol inválido.", "error")
            return render_template("usuarios/form.html", user=user, mode="edit", user_id=user_id, role_choices=ROLE_CHOICES)

        if new_password and len(new_password) < 6:
            flash("La nueva contraseña debe tener al menos 6 caracteres.", "error")
            return render_template("usuarios/form.html", user=user, mode="edit", user_id=user_id, role_choices=ROLE_CHOICES)

        if new_password:
            execute(
                "UPDATE users SET name=?, role=?, active=?, password_hash=? WHERE id=?",
                (name, role, active, generate_password_hash(new_password), user_id),
            )
        else:
            execute("UPDATE users SET name=?, role=?, active=? WHERE id=?", (name, role, active, user_id))

        flash("Usuario actualizado.", "success")
        return redirect(url_for("usuarios.list_view"))

    return render_template("usuarios/form.html", user=user, mode="edit", user_id=user_id, role_choices=ROLE_CHOICES)
