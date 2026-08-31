"""Sincronización automática con Frotcom en segundo plano, cada 2 minutos
(31 ago, pedido de Braulio: "quiero que esta información se actualice en la
página automáticamente cada 2 minutos"). Sin esto, el historial de
posiciones (`vehicle_location_history`, base de "horas manejadas hoy" y los
reportes diarios — ver app/gps_stats.py) solo tendría datos cuando alguien
hace clic manualmente en "Sincronizar", dejando huecos grandes en los
reportes cualquier día que nadie abra esa pantalla.

Cómo funciona: un hilo (`threading.Thread`, daemon) que hace un loop simple
de "sincronizar → dormir N segundos → repetir", corriendo DENTRO del mismo
proceso de la app (no un servicio ni un Cron Job aparte de Render). Esto es
seguro en este proyecto concretamente porque el `Procfile` usa
`gunicorn run:app` SIN `--workers` (un solo proceso — confirmado en
`Procfile`), así que no hay riesgo de que dos procesos distintos disparen
sincronizaciones duplicadas al mismo tiempo. Si en el futuro se cambia el
Start Command para correr con más de un worker de gunicorn, este mecanismo
hay que revisarlo (cada worker arrancaría su propio hilo).

Se puede desactivar o ajustar con la variable de entorno
`FROTCOM_AUTO_SYNC_SECONDS` (ver config.py) — 0 la apaga por completo."""
import logging
import os
import threading
import time

logger = logging.getLogger("frotcom_scheduler")


def start_background_sync(app):
    interval = app.config.get("FROTCOM_AUTO_SYNC_SECONDS", 0)
    if not interval or interval <= 0:
        return
    if not (app.config.get("FROTCOM_USERNAME") and app.config.get("FROTCOM_PASSWORD")):
        # Sin credenciales no hay nada que sincronizar — evita un hilo que
        # solo seguiría logueando "no configurado" cada 2 minutos para
        # siempre.
        return

    # Guarda contra el reloader de desarrollo de Flask (`flask run` con
    # debug=True arranca el proceso dos veces: uno "vigilante" y otro real).
    # En producción (gunicorn, sin reloader) esta variable no existe y el
    # hilo arranca normalmente. Sin este guard, en desarrollo local se
    # duplicaría el hilo y por lo tanto las llamadas a Frotcom.
    if os.environ.get("WERKZEUG_RUN_MAIN") == "false":
        return

    def _loop():
        # Import perezoso: evita un ciclo de imports al cargar app/__init__.py
        # (app.routes.integraciones importa cosas de app.db, que a su vez
        # ya está listo para cuando este hilo realmente corre, pero es más
        # prolijo no importarlo al nivel de módulo de todos modos).
        from app.integrations.frotcom import FrotcomError
        from app.routes.integraciones import perform_frotcom_sync

        while True:
            time.sleep(interval)
            try:
                with app.app_context():
                    result = perform_frotcom_sync()
                    logger.info(
                        "Sincronización automática de Frotcom: %s unidad(es) actualizada(s).",
                        result["matched"],
                    )
            except FrotcomError as exc:
                logger.warning("Sincronización automática de Frotcom falló: %s", exc)
            except Exception:
                # No se deja morir el hilo por un error inesperado (ej. un
                # problema de red puntual) — se loguea y se sigue
                # intentando en el siguiente ciclo.
                logger.exception("Error inesperado en la sincronización automática de Frotcom")

    thread = threading.Thread(target=_loop, name="frotcom-auto-sync", daemon=True)
    thread.start()
    logger.info("Sincronización automática de Frotcom activada cada %s segundos.", interval)
