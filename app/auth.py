"""Autenticación por sesión y control de acceso por rol."""
import functools
import secrets

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from app.db import query_one

bp = Blueprint("auth", __name__, url_prefix="/auth")

# Permisos por módulo. 'view' = puede ver, 'edit' = puede crear/editar/eliminar.
PERMISSIONS = {
    "ADMIN": {"*": {"view", "edit"}},
    "OPERADOR": {
        "dashboard": {"view"},
        "clientes": {"view", "edit"},
        "flota": {"view"},
        "viajes": {"view", "edit"},
        "gastos": {"view", "edit"},
        "mantenimiento": {"view"},
        "facturacion": set(),
        "guias": {"view", "edit"},
        "inspecciones": {"view", "edit"},
        "rutas": {"view"},
        "usuarios": set(),
        "catalogos": set(),
        "integraciones": set(),
    },
}


def can(role, module, action):
    role_perms = PERMISSIONS.get(role, {})
    if "*" in role_perms:
        return action in role_perms["*"]
    return action in role_perms.get(module, set())


@bp.before_app_request
def load_logged_in_user():
    user_id = session.get("user_id")
    if user_id is None:
        g.user = None
    else:
        g.user = query_one(
            "SELECT id, name, email, role, active FROM users WHERE id = ?", (user_id,)
        )
        if g.user is not None and not g.user["active"]:
            session.clear()
            g.user = None


def login_required(view):
    @functools.wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            return redirect(url_for("auth.login", next=request.path))
        return view(**kwargs)

    return wrapped_view


def permission_required(module, action="view"):
    def decorator(view):
        @functools.wraps(view)
        @login_required
        def wrapped_view(**kwargs):
            if not can(g.user["role"], module, action):
                flash("No tienes permiso para acceder a esta sección.", "error")
                return redirect(url_for("dashboard.index"))
            return view(**kwargs)

        return wrapped_view

    return decorator


def get_csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return session["csrf_token"]


def validate_csrf():
    token = session.get("csrf_token")
    form_token = request.form.get("csrf_token")
    return token is not None and form_token is not None and secrets.compare_digest(token, form_token)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user is not None:
        return redirect(url_for("dashboard.index"))

    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = query_one("SELECT * FROM users WHERE email = ?", (email,))

        if user is None or not check_password_hash(user["password_hash"], password):
            error = "Correo o contraseña incorrectos."
        elif not user["active"]:
            error = "Este usuario está desactivado. Contacta al administrador."

        if error is None:
            session.clear()
            session["user_id"] = user["id"]
            next_url = request.args.get("next") or url_for("dashboard.index")
            return redirect(next_url)

        flash(error, "error")

    return render_template("auth/login.html", company_name=current_app.config["COMPANY_NAME"])


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
