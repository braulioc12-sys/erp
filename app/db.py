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
    ("inspections", "checklist_code", "TEXT"),
    ("inspections", "location", "TEXT"),
    ("inspections", "odometer_km", "REAL"),
    ("inspection_items", "section", "TEXT"),
    ("inspection_items", "extra_value", "TEXT"),
    ("expenses", "concept_id", "INTEGER REFERENCES expense_concepts(id)"),
    ("expenses", "document_number", "TEXT"),
    ("expenses", "due_date", "TEXT"),
    ("expenses", "provider_ruc", "TEXT"),
    ("expenses", "provider_name", "TEXT"),
    ("expenses", "currency", "TEXT NOT NULL DEFAULT 'S'"),
    ("expenses", "exchange_rate", "REAL"),
    ("expenses", "expense_advance_id", "INTEGER REFERENCES expense_advances(id)"),
    ("expense_advances", "office", "TEXT"),
    ("expense_advances", "voucher_number", "INTEGER"),
    ("maintenance_record_jobs", "status", "TEXT NOT NULL DEFAULT 'PENDIENTE'"),
    ("maintenance_record_jobs", "mechanic_id", "INTEGER REFERENCES mechanics(id)"),
    ("maintenance_record_jobs", "mechanic_name", "TEXT"),
    ("maintenance_record_jobs", "completed_at", "TEXT"),
    ("maintenance_record_jobs", "mechanic_type", "TEXT"),
    ("mechanics", "mechanic_type", "TEXT NOT NULL DEFAULT 'Otros'"),
    ("maintenance_record_jobs", "mechanic_count", "INTEGER NOT NULL DEFAULT 1"),
    ("vehicles", "owner", "TEXT"),
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


def get_setting(key, default=None):
    """Lee un ajuste general (tabla app_settings, clave/valor). Devuelve
    `default` si todavía no se ha guardado ese ajuste."""
    row = query_one("SELECT value FROM app_settings WHERE key = ?", (key,))
    return row["value"] if row is not None else default


def set_setting(key, value):
    execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
