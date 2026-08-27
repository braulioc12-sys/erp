"""Capa de acceso a datos (SQLite) para el ERP de Transporte."""
import sqlite3
from pathlib import Path
from flask import current_app, g


def get_db():
    """Devuelve la conexión SQLite de la petición actual (se crea una vez por request)."""
    if "db" not in g:
        g.db = sqlite3.connect(
            current_app.config["DATABASE_PATH"],
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# Columnas agregadas a tablas ya existentes en versiones posteriores del
# sistema. `CREATE TABLE IF NOT EXISTS` (usado en schema.sql) no modifica
# una tabla que ya existe, así que en una base de datos que viene de una
# versión anterior (por ejemplo, con disco persistente en producción) esa
# columna nueva no aparecería sola. Esta lista la agrega si falta, sin
# tocar nada más. En hosting con disco efímero (ver README) esto no hace
# falta porque la base se recrea entera en cada despliegue, pero se deja
# aquí para cuando se use un disco persistente con datos reales.
COLUMN_MIGRATIONS = [
    ("vehicles", "vehicle_type", "TEXT NOT NULL DEFAULT 'CAMION'"),
    ("vehicles", "soat_expiry", "TEXT"),
    ("vehicles", "technical_review_expiry", "TEXT"),
    ("drivers", "medical_exam_date", "TEXT"),
    ("drivers", "medical_exam_expiry", "TEXT"),
    ("drivers", "backus_driving_exam_date", "TEXT"),
    ("drivers", "backus_driving_exam_expiry", "TEXT"),
    ("drivers", "backus_training_date", "TEXT"),
    ("drivers", "backus_training_expiry", "TEXT"),
    ("drivers", "dds_date", "TEXT"),
    ("drivers", "dds_expiry", "TEXT"),
    ("trips", "driver_commission", "REAL NOT NULL DEFAULT 0"),
    ("routes", "default_commission_amount", "REAL NOT NULL DEFAULT 0"),
]


def _apply_column_migrations(conn):
    for table, column, ddl in COLUMN_MIGRATIONS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db(app):
    """Crea las tablas si no existen, usando app/schema.sql."""
    db_path = Path(app.config["DATABASE_PATH"])
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    schema_path = Path(__file__).parent / "schema.sql"
    with open(schema_path, "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    _apply_column_migrations(conn)
    conn.commit()
    conn.close()


def register_db(app):
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db(app)


def query_all(sql, params=()):
    return get_db().execute(sql, params).fetchall()


def query_one(sql, params=()):
    return get_db().execute(sql, params).fetchone()


def execute(sql, params=()):
    """Ejecuta INSERT/UPDATE/DELETE y hace commit. Devuelve el lastrowid."""
    db = get_db()
    cur = db.execute(sql, params)
    db.commit()
    return cur.lastrowid
