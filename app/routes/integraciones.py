import json

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from app.auth import permission_required, validate_csrf
from app.db import get_db, query_all
from app.helpers import today_str
from app.integrations.frotcom import FrotcomError, build_client_from_config

bp = Blueprint("integraciones", __name__, url_prefix="/configuracion/integraciones")

# Cuántos IDs se listan como máximo en un solo mensaje de flash. Antes era
# 15 (bastaba cuando Frotcom solo devolvía 15 unidades), pero desde que se
# agregó el intento con kind=A la cuenta real subió a 49+ (31 ago) — con 15
# Braulio tenía que sincronizar varias veces para ver toda la lista. 80 deja
# margen sobre el tamaño real de la flota (50 tractos) sin volver el
# mensaje ilegible.
MAX_IDS_EN_MENSAJE = 80


@bp.route("")
@permission_required("integraciones", "view")
def index():
    client = build_client_from_config(current_app.config)
    vehicles = query_all(
        """SELECT v.id, v.plate, v.gps_external_id, v.current_km, v.current_km_updated_at,
                  l.latitude, l.longitude, l.speed_kmh, l.recorded_at, l.updated_at as location_updated_at
           FROM vehicles v
           LEFT JOIN vehicle_locations l ON l.vehicle_id = v.id
           ORDER BY v.plate"""
    )
    return render_template(
        "integraciones/index.html", vehicles=vehicles, configured=client.is_configured(),
    )


@bp.route("/frotcom/sincronizar", methods=["POST"])
@permission_required("integraciones", "edit")
def sync_frotcom():
    if not validate_csrf():
        flash("Sesión expirada, intenta de nuevo.", "error")
        return redirect(url_for("integraciones.index"))

    client = build_client_from_config(current_app.config)
    if not client.is_configured():
        flash(
            "Frotcom no está configurado todavía. Define FROTCOM_BASE_URL, FROTCOM_USERNAME "
            "y FROTCOM_PASSWORD en las variables de entorno (ver README) y vuelve a intentar.",
            "error",
        )
        return redirect(url_for("integraciones.index"))

    try:
        positions = client.get_vehicle_positions()
    except FrotcomError as exc:
        flash(f"No se pudo sincronizar con Frotcom: {exc}", "error")
        return redirect(url_for("integraciones.index"))

    vehicles = query_all("SELECT id, gps_external_id FROM vehicles WHERE gps_external_id IS NOT NULL")
    by_external_id = {v["gps_external_id"]: v["id"] for v in vehicles}

    db = get_db()
    matched = 0
    for pos in positions:
        vehicle_id = by_external_id.get(pos["external_id"])
        if not vehicle_id:
            continue
        db.execute(
            """INSERT INTO vehicle_locations (vehicle_id, latitude, longitude, speed_kmh, heading, odometer_km, recorded_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(vehicle_id) DO UPDATE SET
                 latitude=excluded.latitude, longitude=excluded.longitude, speed_kmh=excluded.speed_kmh,
                 heading=excluded.heading, odometer_km=excluded.odometer_km, recorded_at=excluded.recorded_at,
                 updated_at=datetime('now')""",
            (
                vehicle_id, pos["latitude"], pos["longitude"], pos["speed_kmh"],
                pos["heading"], pos["odometer_km"], pos["recorded_at"],
            ),
        )
        if pos.get("odometer_km"):
            db.execute(
                "UPDATE vehicles SET current_km = ?, current_km_updated_at = ? WHERE id = ?",
                (pos["odometer_km"], today_str(), vehicle_id),
            )
        matched += 1
    db.commit()

    # IDs que Frotcom sí devolvió pero que ninguna unidad tiene configurados
    # todavía — se muestran tanto si no se sincronizó nada (para diagnosticar
    # un desfase de formato) como si ya se sincronizó algo (para poder mapear
    # el resto de la flota de una sola vez, sin repetir "Sincronizar" unidad
    # por unidad). 31 ago, tras el primer intento real de sincronización.
    frotcom_ids = sorted({p["external_id"] for p in positions if p.get("external_id")})
    configured_ids = sorted(by_external_id.keys())
    unmatched_frotcom_ids = [fid for fid in frotcom_ids if fid not in by_external_id]

    # Si Frotcom trae algo reconocible como placa/nombre por vehículo (ver
    # "label" en get_vehicle_positions), lo mostramos junto al id — así no
    # hace falta adivinar a qué camión corresponde cada id interno de
    # Frotcom (31 ago: los ids reales de Braulio, ej. "190119", no se
    # parecen en nada a una placa).
    label_by_id = {p["external_id"]: p["label"] for p in positions if p.get("external_id") and p.get("label")}

    def _fmt_id(fid):
        return f"{fid} ({label_by_id[fid]})" if fid in label_by_id else fid

    # Diagnóstico del experimento "kind=A" (ver get_vehicle_positions en
    # frotcom.py): si Frotcom devolvió menos unidades de las que Braulio
    # espera (31 ago: 15 vs 50+ reales), probamos si el resto está
    # registrado como "Asset" en vez de "Vehicle". Se muestra el
    # resultado del intento aquí para no depender de logs de Render.
    if client.last_asset_fetch_error:
        flash(f"Aviso: el intento adicional de traer unidades tipo 'Asset' (carretas) falló: {client.last_asset_fetch_error}", "info")
    elif client.last_asset_fetch_count is not None:
        # Se muestra también el caso "0 nuevas" (antes quedaba en silencio
        # porque 0 es "falsy" en Python) — así se sabe que el intento SÍ se
        # hizo y no aportó nada, en vez de no saber si se intentó.
        if client.last_asset_fetch_count:
            flash(f"Además, Frotcom devolvió {client.last_asset_fetch_count} unidad(es) más al pedirlas como tipo 'Asset' (posibles carretas/semirremolques).", "info")
        else:
            flash("Además se probó pedir unidades tipo 'Asset' (carretas) por separado: Frotcom no devolvió ninguna unidad nueva por ese lado.", "info")

    if matched:
        flash(f"Sincronizado: {matched} unidad(es) actualizada(s) desde Frotcom.", "success")
        if unmatched_frotcom_ids:
            flash(
                f"Frotcom también tiene {len(unmatched_frotcom_ids)} unidad(es) más sin mapear "
                f'todavía en Flota. IDs pendientes: '
                f'{", ".join(_fmt_id(f) for f in unmatched_frotcom_ids[:MAX_IDS_EN_MENSAJE])}'
                f'{" (y " + str(len(unmatched_frotcom_ids) - MAX_IDS_EN_MENSAJE) + " más, no entraron en este mensaje)" if len(unmatched_frotcom_ids) > MAX_IDS_EN_MENSAJE else ""}. '
                'Cópialos en el campo "ID en el proveedor de GPS" de la unidad que corresponda '
                "(Flota → editar unidad) y vuelve a sincronizar.",
                "info",
            )
    else:
        # En vez de solo decir "no coincide", mostramos los valores reales de
        # los dos lados para que se puedan comparar de un vistazo — así no
        # hace falta ir a revisar logs de Render para saber qué ID usa
        # Frotcom.
        detalle = (
            f' IDs que devolvió Frotcom: '
            f'{", ".join(_fmt_id(f) for f in frotcom_ids[:MAX_IDS_EN_MENSAJE]) or "(ninguno)"}.'
            f' IDs configurados en Flota ("ID en el proveedor de GPS"): '
            f'{", ".join(configured_ids[:MAX_IDS_EN_MENSAJE]) or "(ninguno todavía)"}.'
        )
        flash(
            "Frotcom respondió pero no se pudo asociar ninguna posición a tus unidades. "
            "Revisa que el campo \"ID en el proveedor de GPS\" de cada unidad (en Flota) "
            "coincida exactamente con el identificador que usa Frotcom." + detalle,
            "error",
        )
        if positions and not label_by_id:
            # Ningún campo típico de placa/nombre coincidió — mostramos TODOS
            # los campos crudos del primer vehículo para terminar de
            # confirmar, sin adivinar más, cuál trae la placa real.
            raw_preview = json.dumps(positions[0]["raw"], ensure_ascii=False)[:600]
            flash(f"Campos reales que trae Frotcom por vehículo (el primero, de ejemplo): {raw_preview}", "info")
    return redirect(url_for("integraciones.index"))
