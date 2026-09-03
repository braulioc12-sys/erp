import secrets

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash

from app.auth import ROLE_LABELS, permission_required, validate_csrf
from app.db import USER_ROLES, execute, get_db, query_all, query_one

bp = Blueprint("usuarios", __name__, url_prefix="/usuarios")

# Lista (rol, etiqueta) en el mismo orden que USER_ROLES (app/db.py, fuente
# única de qué roles acepta la base de datos), para los checkboxes del
# formulario — ver app/auth.py ROLE_LABELS/PERMISSIONS para el detalle de
# qué puede ver/editar cada uno.
ROLE_CHOICES = [(r, ROLE_LABELS.get(r, r)) for r in USER_ROLES]


def _selected_roles(form):
    """Lista de roles marcados en el formulario (checkboxes "roles"),
    limitada a valores válidos y sin duplicados, preservando el orden de
    USER_ROLES — 3 sep, pedido de Braulio: un usuario puede tener más de
    un rol a la vez (ej. Almacén y Mecánico)."""
    selected = set(form.getlist("roles"))
    return [r for r in USER_ROLES if r in selected]


def _save_user_roles(user_id, roles):
    """Reemplaza por completo el conjunto de roles de un usuario en
    user_roles (borra los que ya no aplican, agrega los nuevos) — más
    simple y menos propenso a errores que calcular la diferencia fila por
    fila, y el volumen (unas pocas filas por usuario) no lo justifica."""
    db = get_db()
    db.execute("DELETE FROM user_roles WHERE user_id = ?", (user_id,))
    for role in roles:
        db.execute("INSERT INTO user_roles (user_id, role) VALUES (?, ?)", (user_id, role))
    db.commit()


@bp.route("")
@permission_required("usuarios", "view")
def list_view():
    users = query_all("SELECT * FROM users ORDER BY name")
    # Se trae todo user_roles de una vez y se agrupa en Python (en vez de
    # una query por usuario, o GROUP_CONCAT/STRING_AGG — evita depender de
    # una función que difiere entre SQLite y Postgres; mismo criterio ya
    # usado en _commissions_by_driver de app/routes/viajes.py).
    all_roles = query_all("SELECT user_id, role FROM user_roles ORDER BY role")
    roles_by_user = {}
    for r in all_roles:
        roles_by_user.setdefault(r["user_id"], []).append(r["role"])
    # Etiqueta ya armada (ej. "Almacén, Mecánico") por usuario, para no
    # tener que resolver ROLE_LABELS desde la plantilla (Jinja's "map" solo
    # acepta el nombre de un filtro registrado, no un callable cualquiera
    # como dict.get).
    roles_display = {
        u["id"]: ", ".join(ROLE_LABELS.get(r, r) for r in roles_by_user.get(u["id"], [u["role"]]))
        for u in users
    }
    return render_template("usuarios/list.html", users=users, roles_display=roles_display)


@bp.route("/nuevo", methods=["GET", "POST"])
@permission_required("usuarios", "edit")
def new():
    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        roles = _selected_roles(request.form)
        password = request.form.get("password", "")

        errors = []
        if not name or not email:
            errors.append("Nombre y correo son obligatorios.")
        if not roles:
            errors.append("Selecciona al menos un rol.")
        if len(password) < 6:
            errors.append("La contraseña debe tener al menos 6 caracteres.")
        if email and query_one("SELECT id FROM users WHERE email = ?", (email,)):
            errors.append("Ya existe un usuario con ese correo.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "usuarios/form.html", user=request.form, mode="new",
                role_choices=ROLE_CHOICES, selected_roles=roles,
            )

        # "role" (columna vieja) se sigue llenando con el primero de los
        # roles elegidos, como rol "principal"/de respaldo — ver el
        # comentario en schema.sql. Los permisos reales salen de
        # user_roles, insertado aparte con _save_user_roles() (execute()
        # ya hace su propio commit, y necesitamos el id nuevo antes de
        # poder insertar sus roles).
        user_id = execute(
            "INSERT INTO users (name, email, password_hash, role) VALUES (?, ?, ?, ?)",
            (name, email, generate_password_hash(password), roles[0]),
        )
        _save_user_roles(user_id, roles)
        flash("Usuario creado.", "success")
        return redirect(url_for("usuarios.list_view"))

    return render_template(
        "usuarios/form.html", user=None, mode="new",
        suggested_password=secrets.token_urlsafe(6), role_choices=ROLE_CHOICES, selected_roles=[],
    )


@bp.route("/<int:user_id>/editar", methods=["GET", "POST"])
@permission_required("usuarios", "edit")
def edit(user_id):
    user = query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    if user is None:
        abort(404)
    current_roles = [r["role"] for r in query_all("SELECT role FROM user_roles WHERE user_id = ? ORDER BY role", (user_id,))]

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        name = request.form.get("name", "").strip()
        roles = _selected_roles(request.form)
        active = 1 if request.form.get("active") else 0
        new_password = request.form.get("password", "")

        if not roles:
            flash("Selecciona al menos un rol.", "error")
            return render_template(
                "usuarios/form.html", user=user, mode="edit", user_id=user_id,
                role_choices=ROLE_CHOICES, selected_roles=current_roles,
            )

        if new_password and len(new_password) < 6:
            flash("La nueva contraseña debe tener al menos 6 caracteres.", "error")
            return render_template(
                "usuarios/form.html", user=user, mode="edit", user_id=user_id,
                role_choices=ROLE_CHOICES, selected_roles=current_roles,
            )

        db = get_db()
        if new_password:
            db.execute(
                "UPDATE users SET name=?, role=?, active=?, password_hash=? WHERE id=?",
                (name, roles[0], active, generate_password_hash(new_password), user_id),
            )
        else:
            db.execute("UPDATE users SET name=?, role=?, active=? WHERE id=?", (name, roles[0], active, user_id))
        db.commit()
        _save_user_roles(user_id, roles)

        flash("Usuario actualizado.", "success")
        return redirect(url_for("usuarios.list_view"))

    return render_template(
        "usuarios/form.html", user=user, mode="edit", user_id=user_id,
        role_choices=ROLE_CHOICES, selected_roles=current_roles,
    )
