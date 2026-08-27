"""Datos de ejemplo: usuarios, clientes, flota, conductores y viajes.

Se usa desde dos lugares:
- seed.py (script manual: `python seed.py`)
- app/__init__.py (automático al iniciar, solo si la tabla de usuarios está
  vacía y AUTO_SEED_DEMO=1 — ver README, sección "Desplegar en Render").
"""
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash

from app.db import execute, get_db, query_one


def _upsert_user(name, email, password, role):
    existing = query_one("SELECT id FROM users WHERE email = ?", (email,))
    if existing:
        return existing["id"], False
    uid = execute(
        "INSERT INTO users (name, email, password_hash, role) VALUES (?, ?, ?, ?)",
        (name, email, generate_password_hash(password), role),
    )
    return uid, True


# Catálogos por defecto. Los códigos de "expense_type" se mantienen en
# mayúsculas por compatibilidad con instalaciones previas a la versión con
# catálogos editables; "maintenance_type" siempre fue texto libre, así que
# usa nombres directamente legibles.
DEFAULT_CATALOGS = {
    "expense_type": ["COMBUSTIBLE", "PEAJE", "VIATICOS", "MANTENIMIENTO", "OTRO"],
    "maintenance_type": [
        "Cambio de aceite",
        "Revisión general de frenos",
        "Cambio de llantas",
        "Revisión de sistema eléctrico",
        "Otro",
    ],
    "inspection_item": [
        "Llantas y aros",
        "Frenos",
        "Luces y direccionales",
        "Niveles (aceite, refrigerante, freno)",
        "Extintor y botiquín",
        "Cinturones de seguridad",
        "Espejos y limpiaparabrisas",
        "Documentos del vehículo (SOAT, tarjeta de propiedad)",
    ],
}

# Trabajos de mantenimiento con tiempo estimado (minutos), por defecto.
DEFAULT_JOB_TYPES = [
    ("Cambio de aceite", 60),
    ("Cambio de filtro de aire", 30),
    ("Cambio de filtro de aceite", 20),
    ("Revisión y ajuste de frenos", 90),
    ("Cambio de llantas", 45),
    ("Alineamiento y balanceo", 60),
    ("Revisión de sistema eléctrico", 120),
]


def _seed_catalogs():
    db = get_db()
    for category, items in DEFAULT_CATALOGS.items():
        for order, name in enumerate(items):
            db.execute(
                "INSERT OR IGNORE INTO catalog_items (category, name, sort_order) VALUES (?, ?, ?)",
                (category, name, order),
            )
    for order, (name, minutes) in enumerate(DEFAULT_JOB_TYPES):
        db.execute(
            "INSERT OR IGNORE INTO maintenance_job_types (name, estimated_minutes, sort_order) VALUES (?, ?, ?)",
            (name, minutes, order),
        )
    db.commit()


def seed_demo_data(log=print):
    """Crea usuarios, catálogos y datos de ejemplo si todavía no existen. Es
    seguro llamarla varias veces: no duplica nada. Debe llamarse dentro de un
    app_context (con la base de datos ya inicializada)."""
    log("Usuarios:")
    _, created = _upsert_user("Administrador", "admin@erp.local", "admin1234", "ADMIN")
    log(f"  {'creado' if created else 'ya existe'}: admin@erp.local / admin1234 (ADMIN)")
    _, created = _upsert_user("Operador Demo", "operador@erp.local", "operador1234", "OPERADOR")
    log(f"  {'creado' if created else 'ya existe'}: operador@erp.local / operador1234 (OPERADOR)")

    log("Catálogos (tipos de gasto, conceptos de mantenimiento, trabajos, ítems de inspección)...")
    _seed_catalogs()

    log("Rutas frecuentes con viáticos predeterminados...")
    routes = [
        ("Lima", "Trujillo", 350.0),
        ("Arequipa", "Lima", 400.0),
        ("Trujillo", "Piura", 250.0),
        ("Lima", "Ica", 200.0),
    ]
    for origin, destination, amount in routes:
        execute(
            "INSERT OR IGNORE INTO routes (origin, destination, default_expense_amount) VALUES (?, ?, ?)",
            (origin, destination, amount),
        )

    if query_one("SELECT COUNT(*) n FROM clients")["n"] > 0:
        log("Ya existen clientes; se omiten datos de ejemplo adicionales.")
        return

    log("Clientes, flota, conductores y viajes de ejemplo...")
    clients = [
        ("Comercial Andina S.A.C.", "20123456789", "01-555-1010", "contacto@andina.pe", "Av. Argentina 1200, Lima"),
        ("Distribuidora del Sur E.I.R.L.", "20456789123", "01-555-2020", "ventas@delsur.pe", "Av. Los Incas 500, Arequipa"),
        ("Agroindustrias Norte S.A.", "20789123456", "044-555-3030", "logistica@agronorte.pe", "Panamericana Norte km 12, Trujillo"),
    ]
    client_ids = [
        execute("INSERT INTO clients (name, ruc, phone, email, address) VALUES (?, ?, ?, ?, ?)", c)
        for c in clients
    ]

    # (placa, marca, modelo, capacidad_kg, estado, kilometraje_actual)
    vehicles = [
        ("ABC-123", "Volvo", "FH 460", 28000, "ACTIVO", 118500),
        ("XYZ-789", "Scania", "R450", 25000, "ACTIVO", 76200),
        ("DEF-456", "Mercedes-Benz", "Actros", 30000, "MANTENIMIENTO", 142300),
    ]
    vehicle_ids = [
        execute(
            "INSERT INTO vehicles (plate, brand, model, capacity_kg, status, current_km, current_km_updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (*v, datetime.now().strftime("%Y-%m-%d")),
        )
        for v in vehicles
    ]

    drivers = [
        ("Carlos Ramírez", "45678912", "Q12345678", (datetime.now() + timedelta(days=200)).strftime("%Y-%m-%d"), "987654321"),
        ("Luis Fernández", "41234567", "Q87654321", (datetime.now() + timedelta(days=20)).strftime("%Y-%m-%d"), "987123456"),
        ("Jorge Quispe", "47891234", "Q11223344", (datetime.now() + timedelta(days=400)).strftime("%Y-%m-%d"), "912345678"),
    ]
    driver_ids = [
        execute(
            "INSERT INTO drivers (name, document_number, license_number, license_expiry, phone) VALUES (?, ?, ?, ?, ?)",
            d,
        )
        for d in drivers
    ]

    today = datetime.now()
    trips = [
        (client_ids[0], vehicle_ids[0], driver_ids[0], "Lima", "Trujillo", "Electrodomésticos", 8000, -5, "ENTREGADO", 2500),
        (client_ids[1], vehicle_ids[1], driver_ids[1], "Arequipa", "Lima", "Textiles", 5000, -2, "ENTREGADO", 1800),
        (client_ids[2], vehicle_ids[0], driver_ids[2], "Trujillo", "Piura", "Productos agrícolas", 12000, 1, "EN_CURSO", 2200),
        (client_ids[0], vehicle_ids[1], driver_ids[0], "Lima", "Ica", "Materiales de construcción", 9000, 3, "PENDIENTE", 1500),
    ]
    for i, (cid, vid, did, origin, dest, cargo, weight, day_offset, status, rate) in enumerate(trips, start=1):
        scheduled = (today + timedelta(days=min(day_offset, 0))).strftime("%Y-%m-%d")
        delivered = scheduled if status == "ENTREGADO" else None
        code = f"V-{i:04d}"
        execute(
            """INSERT INTO trips (code, client_id, vehicle_id, driver_id, origin, destination,
               cargo_description, cargo_weight_kg, scheduled_date, delivered_date, status, rate)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (code, cid, vid, did, origin, dest, cargo, weight, scheduled, delivered, status, rate),
        )

    execute(
        """INSERT INTO maintenance_records (vehicle_id, type, maintenance_date, cost, description, odometer_km, next_due_date, next_due_km)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            vehicle_ids[2],
            "Revisión general de frenos",
            (today - timedelta(days=3)).strftime("%Y-%m-%d"),
            850.0,
            "Cambio de pastillas y revisión de sistema hidráulico",
            142300,
            (today + timedelta(days=10)).strftime("%Y-%m-%d"),
            143000,
        ),
    )
    # Unidad ABC-123: próxima a su mantenimiento por kilometraje (para ver
    # la alerta funcionando de una vez en la demo).
    execute(
        """INSERT INTO maintenance_records (vehicle_id, type, maintenance_date, cost, description, odometer_km, next_due_km)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            vehicle_ids[0],
            "Cambio de aceite",
            (today - timedelta(days=60)).strftime("%Y-%m-%d"),
            320.0,
            "Cambio de aceite y filtros",
            113000,
            119000,
        ),
    )
    log("Listo. Inicia sesión con:")
    log("  admin@erp.local / admin1234  (Administrador)")
    log("  operador@erp.local / operador1234  (Operador)")
