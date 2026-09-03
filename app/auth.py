"""Autenticación por sesión y control de acceso por rol."""
import functools
import secrets

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from app.db import query_all, query_one

bp = Blueprint("auth", __name__, url_prefix="/auth")

# Permisos por módulo. 'view' = puede ver, 'edit' = puede crear/editar/eliminar.
#
# 3 sep (pedido de Braulio: "definamos los roles de usuario") — se agregaron
# 4 roles especializados (DESPACHADOR/ALMACEN/CONTABILIDAD/MECANICO) y se
# redujo OPERADOR a lo básico del día a día, ahora que esos roles cubren lo
# que antes hacía Operador con Inventarios/Liquidaciones/Facturación. OJO:
# cualquier usuario real que ya tenga rol OPERADOR en producción pierde el
# acceso a Liquidaciones/Inventarios apenas se despliegue esto — hay que
# reasignarlo a Contabilidad/Almacén (o al rol que corresponda) desde
# Usuarios para que no pierda acceso a su trabajo diario.
PERMISSIONS = {
    "ADMIN": {"*": {"view", "edit"}},
    "OPERADOR": {
        # Reducido (3 sep) a lo básico de operación diaria: viajes, clientes
        # y los documentos que salen directo de un viaje (guía, inspección).
        # Flota/Conductores/Rutas/Mantenimiento/Neumáticos se dejan en solo
        # "ver" (como ya estaba) para poder consultarlos al armar un viaje,
        # sin poder editarlos — eso ahora es de Despachador/Mecánico.
        "dashboard": {"view"},
        "clientes": {"view", "edit"},
        "viajes": {"view", "edit"},
        "guias": {"view", "edit"},
        "inspecciones": {"view", "edit"},
        "flota": {"view"},
        "conductores": {"view"},
        "rutas": {"view"},
        "mantenimiento": {"view"},
        "neumaticos": {"view"},
        # Liquidaciones/Facturación/Cotizaciones/Inventarios/Usuarios ahora
        # son de los roles especializados (Contabilidad/Almacén/Admin) —
        # antes de esto, Operador sí tenía Liquidaciones e Inventarios.
        "liquidaciones": set(),
        "facturacion": set(),
        "cotizaciones": set(),
        "inventarios": set(),
        "usuarios": set(),
        "catalogos": set(),
        "integraciones": set(),
    },
    # Programa unidades/conductores y da seguimiento a los viajes del día a
    # día — sin acceso a montos de Facturación/Liquidaciones/Cotizaciones ni
    # a Inventarios (eso es de Contabilidad/Almacén).
    "DESPACHADOR": {
        "dashboard": {"view"},
        "viajes": {"view", "edit"},
        "flota": {"view", "edit"},
        "conductores": {"view", "edit"},
        "rutas": {"view", "edit"},
        "clientes": {"view"},
        "mantenimiento": {"view"},
        "neumaticos": {"view"},
        "liquidaciones": set(),
        "facturacion": set(),
        "cotizaciones": set(),
        "inventarios": set(),
        "guias": set(),
        "inspecciones": set(),
        "usuarios": set(),
        "catalogos": set(),
        "integraciones": set(),
    },
    # Solo el módulo de Inventarios (repuestos, proveedores, compras) — la
    # AUTORIZACIÓN de una orden de compra sigue siendo exclusiva de
    # Administrador (chequeado por rol directamente en
    # purchases_authorize(), no por este permiso de módulo).
    "ALMACEN": {
        "dashboard": {"view"},
        "inventarios": {"view", "edit"},
        "mantenimiento": {"view"},
        "viajes": set(),
        "clientes": set(),
        "flota": set(),
        "conductores": set(),
        "rutas": set(),
        "neumaticos": set(),
        "liquidaciones": set(),
        "facturacion": set(),
        "cotizaciones": set(),
        "guias": set(),
        "inspecciones": set(),
        "usuarios": set(),
        "catalogos": set(),
        "integraciones": set(),
    },
    # Documentos con montos: Liquidaciones, Facturación, Cotizaciones. Ve
    # Viajes/Clientes (para ubicar a qué viaje o cliente corresponde cada
    # documento) e Inventarios en modo solo lectura (para verificar costos
    # de compras), sin poder editar el stock — Braulio: avísame si prefieres
    # que Contabilidad SÍ pueda editar Inventarios.
    "CONTABILIDAD": {
        "dashboard": {"view"},
        "liquidaciones": {"view", "edit"},
        "facturacion": {"view", "edit"},
        "cotizaciones": {"view", "edit"},
        "inventarios": {"view"},
        "viajes": {"view"},
        "clientes": {"view"},
        "flota": set(),
        "conductores": set(),
        "rutas": set(),
        "mantenimiento": set(),
        "neumaticos": set(),
        "guias": set(),
        "inspecciones": set(),
        "usuarios": set(),
        "catalogos": set(),
        "integraciones": set(),
    },
    # Solo el módulo de Mantenimiento (órdenes, trabajos) y ver Neumáticos.
    "MECANICO": {
        "dashboard": {"view"},
        "mantenimiento": {"view", "edit"},
        "neumaticos": {"view"},
        "viajes": set(),
        "clientes": set(),
        "flota": set(),
        "conductores": set(),
        "rutas": set(),
        "liquidaciones": set(),
        "facturacion": set(),
        "cotizaciones": set(),
        "inventarios": set(),
        "guias": set(),
        "inspecciones": set(),
        "usuarios": set(),
        "catalogos": set(),
        "integraciones": set(),
    },
}

# Nombre legible de cada rol, para mostrar en Usuarios (list.html/form.html)
# en vez del valor crudo guardado en la base.
ROLE_LABELS = {
    "ADMIN": "Administrador",
    "OPERADOR": "Operador",
    "DESPACHADOR": "Despachador",
    "ALMACEN": "Almacén",
    "CONTABILIDAD": "Contabilidad",
    "MECANICO": "Mecánico",
}


def can(roles, module, action):
    """`roles` es normalmente `current_user.roles`/`g.user["roles"]` — una
    lista, porque desde el 3 sep un usuario puede tener más de un rol
    (pedido de Braulio: "alguien puede ser almacen y mecanico"). Sus
    permisos son la UNIÓN de lo que permite cada rol asignado (si CUALQUIERA
    de sus roles da acceso, puede). Se acepta también un string suelto (un
    solo rol) por compatibilidad — algún llamado directo, o código viejo que
    no se haya migrado a la lista."""
    if isinstance(roles, str):
        roles = (roles,)
    for role in roles or ():
        role_perms = PERMISSIONS.get(role, {})
        if "*" in role_perms:
            if action in role_perms["*"]:
                return True
        elif action in role_perms.get(module, set()):
            return True
    return False


@bp.before_app_request
def load_logged_in_user():
    user_id = session.get("user_id")
    if user_id is None:
        g.user = None
    else:
        user = query_one(
            "SELECT id, name, email, role, active FROM users WHERE id = ?", (user_id,)
        )
        if user is not None and not user["active"]:
            session.clear()
            g.user = None
        elif user is None:
            g.user = None
        else:
            # 3 sep: los permisos reales vienen de user_roles (múltiples
            # roles por usuario), no de la columna "role" (que se sigue
            # llenando, pero ahora es solo el rol "principal"/de respaldo —
            # ver el comentario en schema.sql). g.user pasa de una
            # sqlite3.Row a un dict mutable para poder agregarle "roles" —
            # el resto del código (current_user.name, g.user["email"], etc.)
            # sigue funcionando igual, un dict soporta lo mismo que una Row.
            roles = [r["role"] for r in query_all("SELECT role FROM user_roles WHERE user_id = ? ORDER BY role", (user_id,))]
            if not roles:
                # No debería pasar tras el backfill de _backfill_user_roles_*
                # (app/db.py), pero por si acaso: sin esto, un usuario sin
                # ninguna fila en user_roles se quedaría sin ningún permiso.
                roles = [user["role"]]
            user_dict = dict(user)
            user_dict["roles"] = roles
            g.user = user_dict


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
            if not can(g.user["roles"], module, action):
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
