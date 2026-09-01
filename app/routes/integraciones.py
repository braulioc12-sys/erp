import json
import logging
import threading
import time
from datetime import datetime, timedelta

from flask import Blueprint, Response, current_app, flash, redirect, render_template, request, url_for

from app.auth import permission_required, validate_csrf
from app.db import execute, get_db, query_all, query_one
from app.gps_stats import combined_daily_stats, daily_stats_all
from app.helpers import parse_date, today_str
from app.integrations.frotcom import FrotcomError, build_client_from_config

bp = Blueprint("integraciones", __name__, url_prefix="/configuracion/integraciones")
logger = logging.getLogger("frotcom_trips")

# Cuántos IDs se listan como máximo en un solo mensaje de flash. Antes era
# 15 (bastaba cuando Frotcom solo devolvía 15 unidades), pero desde que se
# agregó el intento con kind=A la cuenta real subió a 49+ (31 ago) — con 15
# Braulio tenía que sincronizar varias veces para ver toda la lista. 80 deja
# margen sobre el tamaño real de la flota (50 tractos) sin volver el
# mensaje ilegible.
MAX_IDS_EN_MENSAJE = 80

# Límite real del endpoint de viajes de Frotcom (ver get_vehicle_trips en
# app/integrations/frotcom.py): un pedido no puede cubrir más de 7 días, así
# que un rango más largo pedido por Braulio se parte en tramos de este
# tamaño antes de llamar la API, uno por uno.
TRIPS_CHUNK_DAYS = 7

# Pausa entre llamadas a la API de viajes (31 ago) — el límite real de
# "rate limit" de Frotcom no está confirmado (ver frotcom.py), así que se
# deja un margen conservador entre cada llamada en vez de dispararlas todas
# seguidas. Si Braulio confirma que Frotcom permite más, se puede bajar.
TRIPS_API_PAUSE_SECONDS = 0.3


def _chunk_date_range(date_from, date_to, days=TRIPS_CHUNK_DAYS):
    """Parte [date_from, date_to) en tramos de máximo `days` días."""
    chunks = []
    cur = date_from
    while cur < date_to:
        chunk_end = min(cur + timedelta(days=days), date_to)
        chunks.append((cur, chunk_end))
        cur = chunk_end
    return chunks


def _upsert_trip(db, vehicle_id, trip):
    """Guarda (o actualiza si ya existía) un viaje de Frotcom — se
    identifica por frotcom_trip_id, no por (vehicle_id, fecha), porque un
    viaje en curso puede volver a aparecer en una llamada posterior con el
    `ended_at` ya actualizado (ver docstring de get_vehicle_trips)."""
    db.execute(
        """INSERT INTO vehicle_trips (
               vehicle_id, frotcom_trip_id, started_at, ended_at, start_place, start_address,
               start_latitude, start_longitude, start_odometer_km, end_place, end_address,
               end_latitude, end_longitude, end_odometer_km, driver_name, drive_time_sec,
               trip_duration_sec, mileage_km, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(frotcom_trip_id) DO UPDATE SET
               started_at=excluded.started_at, ended_at=excluded.ended_at,
               start_place=excluded.start_place, start_address=excluded.start_address,
               start_latitude=excluded.start_latitude, start_longitude=excluded.start_longitude,
               start_odometer_km=excluded.start_odometer_km, end_place=excluded.end_place,
               end_address=excluded.end_address, end_latitude=excluded.end_latitude,
               end_longitude=excluded.end_longitude, end_odometer_km=excluded.end_odometer_km,
               driver_name=excluded.driver_name, drive_time_sec=excluded.drive_time_sec,
               trip_duration_sec=excluded.trip_duration_sec, mileage_km=excluded.mileage_km,
               updated_at=datetime('now')""",
        (
            vehicle_id, trip["frotcom_trip_id"], trip["started_at"], trip["ended_at"],
            trip["start_place"], trip["start_address"], trip["start_latitude"], trip["start_longitude"],
            trip["start_odometer_km"], trip["end_place"], trip["end_address"],
            trip["end_latitude"], trip["end_longitude"], trip["end_odometer_km"],
            trip["driver_name"], trip["drive_time_sec"], trip["trip_duration_sec"], trip["mileage_km"],
        ),
    )


def perform_trips_backfill(app, job_id, date_from, date_to):
    """Corre en un hilo de segundo plano (lanzado desde la vista
    `trips_history`, ver más abajo) — trae de Frotcom los viajes de TODAS
    las unidades con GPS configurado, entre date_from y date_to (objetos
    datetime), y los guarda en vehicle_trips. Actualiza
    frotcom_trip_import_jobs en cada paso para que la pantalla de
    "Historial de viajes" pueda mostrar el avance sin bloquear la petición
    HTTP original — con 50 unidades x varios tramos de 7 días, esto puede
    tardar varios minutos, más de lo que aguanta una sola petición web."""
    with app.app_context():
        db = get_db()
        try:
            client = build_client_from_config(app.config)
            if not client.is_configured():
                raise FrotcomError("Frotcom no está configurado.")
            vehicles = query_all(
                "SELECT id, gps_external_id FROM vehicles WHERE gps_external_id IS NOT NULL"
            )
            db.execute(
                "UPDATE frotcom_trip_import_jobs SET vehicles_total=?, status='EN_PROGRESO' WHERE id=?",
                (len(vehicles), job_id),
            )
            db.commit()
            chunks = _chunk_date_range(date_from, date_to)
            trips_imported = 0
            errors_seen = 0
            # Primer error real de la API que se encuentre en esta corrida
            # (31 ago) — se guarda en el job para que sea visible en la
            # pantalla de "Historial de viajes" sin necesitar logs de
            # Render. Un job puede terminar "COMPLETADO" con 0 viajes
            # importados si TODAS las llamadas a Frotcom fallaron (ej. un
            # formato de fecha rechazado) — sin esto, esa causa quedaba
            # invisible para Braulio.
            sample_error = None
            for vehicle in vehicles:
                for chunk_from, chunk_to in chunks:
                    try:
                        trips = client.get_vehicle_trips(vehicle["gps_external_id"], chunk_from, chunk_to)
                    except FrotcomError as exc:
                        errors_seen += 1
                        if sample_error is None:
                            sample_error = f"Unidad {vehicle['gps_external_id']}: {exc}"
                            db.execute(
                                "UPDATE frotcom_trip_import_jobs SET sample_error=? WHERE id=?",
                                (sample_error[:500], job_id),
                            )
                            db.commit()
                        logger.warning(
                            "No se pudo traer viajes de la unidad %s (%s a %s): %s",
                            vehicle["gps_external_id"], chunk_from, chunk_to, exc,
                        )
                        continue
                    for trip in trips:
                        _upsert_trip(db, vehicle["id"], trip)
                        trips_imported += 1
                    time.sleep(TRIPS_API_PAUSE_SECONDS)
                db.execute(
                    "UPDATE frotcom_trip_import_jobs SET vehicles_done = vehicles_done + 1, trips_imported=? WHERE id=?",
                    (trips_imported, job_id),
                )
                db.commit()
            db.execute(
                "UPDATE frotcom_trip_import_jobs SET status='COMPLETADO', finished_at=datetime('now') WHERE id=?",
                (job_id,),
            )
            db.commit()
            logger.info(
                "Importación de viajes #%s completada: %s viajes, %s llamadas fallidas.",
                job_id, trips_imported, errors_seen,
            )
        except Exception as exc:
            logger.exception("Error en la importación de viajes #%s", job_id)
            db.execute(
                "UPDATE frotcom_trip_import_jobs SET status='ERROR', error_message=?, finished_at=datetime('now') WHERE id=?",
                (str(exc)[:500], job_id),
            )
            db.commit()


def perform_frotcom_sync(client=None):
    """Hace el ciclo completo de sincronización con Frotcom (login, traer
    posiciones, guardar en vehicle_locations Y en el historial) y devuelve
    todo lo necesario para armar los mensajes de diagnóstico. Separado de
    la vista `sync_frotcom()` (31 ago) para que el mismo código sirva tanto
    al botón manual "Sincronizar" como a la sincronización automática en
    segundo plano cada 2 minutos (ver app/scheduler.py) — antes esta lógica
    vivía solo dentro de la vista, atada a `flash()`/`request`, que no
    existen fuera de una petición HTTP real.

    Devuelve un dict: {matched, positions, by_external_id, client}. Puede
    lanzar FrotcomError (no configurado, o error de red/API) — quien llama
    decide cómo mostrarlo (flash en la vista, log en el scheduler)."""
    if client is None:
        client = build_client_from_config(current_app.config)
    if not client.is_configured():
        raise FrotcomError(
            "Frotcom no está configurado todavía. Define FROTCOM_BASE_URL, FROTCOM_USERNAME "
            "y FROTCOM_PASSWORD en las variables de entorno (ver README)."
        )

    positions = client.get_vehicle_positions()

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
        # Historial (31 ago): a diferencia de vehicle_locations (se
        # sobrescribe), acá se agrega una fila nueva en cada sincronización
        # — es lo que permite calcular horas manejadas/km avanzados por día
        # más abajo (ver app/gps_stats.py), algo que una sola fila por
        # unidad no puede responder.
        db.execute(
            """INSERT INTO vehicle_location_history (vehicle_id, latitude, longitude, speed_kmh, odometer_km, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (vehicle_id, pos["latitude"], pos["longitude"], pos["speed_kmh"], pos["odometer_km"], pos["recorded_at"]),
        )
        if pos.get("odometer_km"):
            db.execute(
                "UPDATE vehicles SET current_km = ?, current_km_updated_at = ? WHERE id = ?",
                (pos["odometer_km"], today_str(), vehicle_id),
            )
        matched += 1
    db.commit()
    return {"matched": matched, "positions": positions, "by_external_id": by_external_id, "client": client}


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
    # Horas manejadas y km avanzados HOY por unidad (31 ago, pedido de
    # Braulio). Preferimos los viajes ya calculados por Frotcom
    # (vehicle_trips, si ya se importaron con "Traer historial") y solo
    # caemos al estimado por posiciones sueltas cuando no hay viajes
    # importados ese día todavía — ver app/gps_stats.py.
    stats_by_vehicle = combined_daily_stats(today_str())
    return render_template(
        "integraciones/index.html", vehicles=vehicles, configured=client.is_configured(),
        stats_by_vehicle=stats_by_vehicle, auto_sync_enabled=current_app.config.get("FROTCOM_AUTO_SYNC_SECONDS", 0) > 0,
    )


@bp.route("/frotcom/sincronizar", methods=["POST"])
@permission_required("integraciones", "edit")
def sync_frotcom():
    if not validate_csrf():
        flash("Sesión expirada, intenta de nuevo.", "error")
        return redirect(url_for("integraciones.index"))

    client = build_client_from_config(current_app.config)
    try:
        result = perform_frotcom_sync(client)
    except FrotcomError as exc:
        flash(f"No se pudo sincronizar con Frotcom: {exc}", "error")
        return redirect(url_for("integraciones.index"))

    matched = result["matched"]
    positions = result["positions"]
    by_external_id = result["by_external_id"]

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


@bp.route("/reportes")
@permission_required("integraciones", "view")
def daily_report():
    """Reporte diario de horas manejadas y km avanzados por unidad (31 ago,
    pedido de Braulio). Preferimos los viajes ya calculados por Frotcom
    (importados con "Traer historial") y solo caemos al estimado por
    posiciones sueltas cuando no hay viajes para ese día — ver
    app/gps_stats.py."""
    date = request.args.get("date") or today_str()
    stats_by_vehicle = combined_daily_stats(date)
    vehicles = query_all("SELECT id, plate FROM vehicles ORDER BY plate")
    rows = [
        {
            "plate": v["plate"],
            "hours": stats_by_vehicle.get(v["id"], {}).get("hours", 0.0),
            "km": stats_by_vehicle.get(v["id"], {}).get("km", 0.0),
            "points": stats_by_vehicle.get(v["id"], {}).get("points", 0),
            "source": stats_by_vehicle.get(v["id"], {}).get("source", ""),
        }
        for v in vehicles
    ]
    return render_template("integraciones/reportes.html", date=date, rows=rows)


@bp.route("/reportes/exportar")
@permission_required("integraciones", "view")
def daily_report_export():
    from app.reports import build_gps_daily_workbook

    date = request.args.get("date") or today_str()
    stats_by_vehicle = combined_daily_stats(date)
    vehicles = query_all("SELECT id, plate FROM vehicles ORDER BY plate")
    rows = [
        {
            "plate": v["plate"],
            "hours": stats_by_vehicle.get(v["id"], {}).get("hours", 0.0),
            "km": stats_by_vehicle.get(v["id"], {}).get("km", 0.0),
        }
        for v in vehicles
    ]
    buffer = build_gps_daily_workbook(rows, company_name=current_app.config["COMPANY_NAME"], date=date)
    return Response(
        buffer.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=reporte_gps_{date}.xlsx"},
    )


@bp.route("/historial", methods=["GET", "POST"])
@permission_required("integraciones", "edit")
def trips_history():
    """Trae de Frotcom el historial de viajes (GET /v2/vehicles/{id}/trips,
    31 ago) de TODAS las unidades con GPS configurado, para un rango de
    fechas que elige Braulio — sirve para (a) rellenar reportes de días
    anteriores a que existiera esta función, y (b) es la base de datos que
    va a necesitar más adelante el reporte de cumplimiento de hoja de ruta
    (origen/destino/horarios reales de cada viaje).

    Corre en un hilo de segundo plano (ver perform_trips_backfill) porque
    puede tardar varios minutos con una flota de 50 unidades — la petición
    HTTP solo lo dispara y redirige, no espera a que termine."""
    if request.method == "POST":
        if not validate_csrf():
            flash("Sesión expirada, intenta de nuevo.", "error")
            return redirect(url_for("integraciones.trips_history"))

        date_from_str = parse_date(request.form.get("date_from"))
        date_to_str = parse_date(request.form.get("date_to"))
        if not date_from_str or not date_to_str:
            flash("Elige una fecha 'desde' y 'hasta' válidas.", "error")
            return redirect(url_for("integraciones.trips_history"))

        dt_from = datetime.strptime(date_from_str, "%Y-%m-%d")
        # "hasta" es inclusivo del día elegido, así que el rango real le
        # pide a Frotcom hasta el final de ese día (medianoche del día
        # siguiente) — si no, se perdería el propio día "hasta".
        dt_to = datetime.strptime(date_to_str, "%Y-%m-%d") + timedelta(days=1)
        if dt_to <= dt_from:
            flash("La fecha 'hasta' debe ser igual o posterior a 'desde'.", "error")
            return redirect(url_for("integraciones.trips_history"))

        # Solo una importación a la vez (31 ago) — corren en el mismo hilo
        # único de la app (ver justificación de un solo worker de gunicorn
        # en app/scheduler.py), así que dos al mismo tiempo solo
        # competirían por la misma conexión sin ninguna ventaja.
        running = query_one(
            "SELECT id FROM frotcom_trip_import_jobs WHERE status IN ('PENDIENTE', 'EN_PROGRESO') ORDER BY id DESC LIMIT 1"
        )
        if running:
            flash("Ya hay una importación de viajes en curso — espera a que termine antes de iniciar otra.", "error")
            return redirect(url_for("integraciones.trips_history"))

        job_id = execute(
            "INSERT INTO frotcom_trip_import_jobs (date_from, date_to, status) VALUES (?, ?, 'PENDIENTE')",
            (date_from_str, date_to_str),
        )
        app_obj = current_app._get_current_object()
        thread = threading.Thread(
            target=perform_trips_backfill, args=(app_obj, job_id, dt_from, dt_to),
            name=f"frotcom-trips-backfill-{job_id}", daemon=True,
        )
        thread.start()
        flash(
            f"Importación de viajes iniciada ({date_from_str} a {date_to_str}). "
            "Puede tardar varios minutos con toda la flota — actualiza esta página para ver el avance.",
            "success",
        )
        return redirect(url_for("integraciones.trips_history"))

    jobs = query_all("SELECT * FROM frotcom_trip_import_jobs ORDER BY id DESC LIMIT 10")
    return render_template("integraciones/historial.html", jobs=jobs, today=today_str())
