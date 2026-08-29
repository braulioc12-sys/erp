import os

from flask import Flask, g, redirect, url_for

from config import Config


def create_app(config_object=Config):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_object)

    from app.db import register_db

    register_db(app)

    # En hosting gratuito (p. ej. Render free) el disco es efímero y se
    # reinicia en cada redeploy o reinicio del servicio. Para que el enlace
    # de demostración siempre tenga con qué iniciar sesión, si la tabla de
    # usuarios queda vacía se vuelve a cargar la data de ejemplo automáticamente.
    # Pon AUTO_SEED_DEMO=0 en tus variables de entorno para desactivar esto
    # (recomendado una vez que cargues datos reales del negocio).
    if os.environ.get("AUTO_SEED_DEMO", "1") == "1":
        from app.db import query_one
        from app.seed_data import seed_demo_data

        with app.app_context():
            if query_one("SELECT COUNT(*) n FROM users")["n"] == 0:
                seed_demo_data(log=app.logger.info)

    from app import auth

    app.register_blueprint(auth.bp)

    from app.routes import (
        dashboard,
        clientes,
        flota,
        conductores,
        viajes,
        liquidaciones,
        inventarios,
        mantenimiento,
        facturacion,
        guias,
        inspecciones,
        rutas,
        neumaticos,
        usuarios,
        catalogos,
        integraciones,
    )

    app.register_blueprint(dashboard.bp)
    app.register_blueprint(clientes.bp)
    app.register_blueprint(flota.bp)
    app.register_blueprint(conductores.bp)
    app.register_blueprint(viajes.bp)
    app.register_blueprint(liquidaciones.bp)
    app.register_blueprint(inventarios.bp)
    app.register_blueprint(mantenimiento.bp)
    app.register_blueprint(facturacion.bp)
    app.register_blueprint(guias.bp)
    app.register_blueprint(inspecciones.bp)
    app.register_blueprint(rutas.bp)
    app.register_blueprint(neumaticos.bp)
    app.register_blueprint(usuarios.bp)
    app.register_blueprint(catalogos.bp)
    app.register_blueprint(integraciones.bp)

    @app.route("/")
    def index():
        return redirect(url_for("dashboard.index"))

    from app.helpers import money, pretty_label

    app.jinja_env.filters["money"] = money
    app.jinja_env.filters["pretty"] = pretty_label

    @app.context_processor
    def inject_globals():
        from app.auth import can, get_csrf_token

        return {
            "current_user": g.get("user"),
            "can": can,
            "csrf_token": get_csrf_token,
            "company_name": app.config["COMPANY_NAME"],
        }

    @app.errorhandler(404)
    def not_found(e):
        from flask import render_template

        return render_template("errors/404.html"), 404

    return app
