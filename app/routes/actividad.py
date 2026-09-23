"""Pantalla de Actividad -- pedido de Braulio (22 sep): "Hay manera de que
yo como administrador, pueda saber quien ha hecho cada cosa? Es decir que
usuario creo el viaje, subio algun doc, borro guia o genero guias, etc?".

Es de solo lectura: lista lo que ya quedó registrado por log_activity()
(ver app/audit.py) en cada módulo instrumentado, con filtros por usuario,
módulo, tipo de acción y rango de fechas. Por defecto es un módulo de
Administrador (ver "actividad" en PERMISSIONS, app/auth.py), aunque se
puede dar de forma puntual a otro usuario desde Usuarios > Permisos
específicos, igual que cualquier otro módulo del catálogo.

No tiene paginación (todavía) -- como el resto de pantallas de este
sistema no la usan, se sigue el mismo criterio simple: se muestran como
máximo las últimas 300 filas que cumplan los filtros elegidos, con un aviso
si el total real supera ese límite (hay que acotar más los filtros para ver
el resto)."""
from flask import Blueprint, render_template, request

from app.audit import ACTION_LABELS
from app.auth import permission_required
from app.db import query_all, query_one
from app.permissions_catalog import MODULE_LABELS

bp = Blueprint("actividad", __name__, url_prefix="/actividad")

MAX_ROWS = 300


@bp.route("")
@permission_required("actividad", "view")
def list_view():
    module_filter = request.args.get("module", "").strip()
    action_filter = request.args.get("action", "").strip()
    user_id_filter = request.args.get("user_id", "").strip()
    from_date = request.args.get("from_date", "").strip()
    to_date = request.args.get("to_date", "").strip()
    q = request.args.get("q", "").strip()

    where = []
    params = []
    if module_filter:
        where.append("module = ?")
        params.append(module_filter)
    if action_filter:
        where.append("action = ?")
        params.append(action_filter)
    if user_id_filter:
        where.append("user_id = ?")
        params.append(user_id_filter)
    if from_date:
        where.append("created_at >= ?")
        params.append(from_date + " 00:00:00")
    if to_date:
        where.append("created_at <= ?")
        params.append(to_date + " 23:59:59")
    if q:
        where.append("LOWER(label) LIKE LOWER(?)")
        params.append(f"%{q}%")

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    total = query_one(f"SELECT COUNT(*) n FROM activity_log {where_sql}", tuple(params))["n"]
    rows = query_all(
        f"SELECT * FROM activity_log {where_sql} ORDER BY id DESC LIMIT {MAX_ROWS}",
        tuple(params),
    )

    # Para los desplegables de filtro: módulos y usuarios que REALMENTE
    # tienen alguna actividad registrada (no todo el catálogo de módulos),
    # así no se llenan de opciones que nunca van a traer resultados.
    modules_with_activity = [r["module"] for r in query_all("SELECT DISTINCT module FROM activity_log ORDER BY module")]
    actions_with_activity = [r["action"] for r in query_all("SELECT DISTINCT action FROM activity_log ORDER BY action")]
    users_with_activity = query_all(
        "SELECT DISTINCT user_id, user_name FROM activity_log WHERE user_id IS NOT NULL ORDER BY user_name"
    )

    return render_template(
        "actividad/list.html",
        rows=rows, total=total, max_rows=MAX_ROWS,
        module_filter=module_filter, action_filter=action_filter,
        user_id_filter=user_id_filter, from_date=from_date, to_date=to_date, q=q,
        modules_with_activity=modules_with_activity, actions_with_activity=actions_with_activity,
        users_with_activity=users_with_activity, module_labels=MODULE_LABELS,
        action_labels=ACTION_LABELS,
    )
