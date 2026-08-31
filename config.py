import os
from pathlib import Path

BASE_DIR = Path(__file__).parent


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "cambia-esta-clave-en-produccion")
    DATABASE_PATH = os.environ.get(
        "DATABASE_PATH", str(BASE_DIR / "instance" / "erp.db")
    )
    # Si se define, la app usa PostgreSQL (ej. Amazon RDS) en vez de SQLite
    # — ver app/db.py y README, sección "Base de datos persistente en AWS
    # (RDS + S3)". Vacío por defecto: sigue usando DATABASE_PATH como
    # siempre.
    DATABASE_URL = os.environ.get("DATABASE_URL", "")

    # Si se define, los comprobantes de gastos (Liquidaciones) se guardan en
    # este bucket de Amazon S3 en vez de en disco local — ver app/storage.py
    # y el mismo apartado del README. Las credenciales (AWS_ACCESS_KEY_ID /
    # AWS_SECRET_ACCESS_KEY / AWS_DEFAULT_REGION) no se leen aquí: boto3 las
    # toma solo, directo de esas variables de entorno estándar.
    AWS_S3_BUCKET = os.environ.get("AWS_S3_BUCKET", "")
    AWS_S3_PREFIX = os.environ.get("AWS_S3_PREFIX", "comprobantes")

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # En producción detrás de HTTPS, activa esto en tu entorno:
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "0") == "1"
    COMPANY_NAME = os.environ.get("COMPANY_NAME", "Harraso Transport")

    # Integración con Frotcom (GPS). Ver README, sección "Integración con
    # Frotcom (GPS)" para cómo obtener estas credenciales.
    FROTCOM_BASE_URL = os.environ.get("FROTCOM_BASE_URL", "")
    FROTCOM_USERNAME = os.environ.get("FROTCOM_USERNAME", "")
    FROTCOM_PASSWORD = os.environ.get("FROTCOM_PASSWORD", "")
    # Cada cuántos segundos se sincroniza solo con Frotcom en segundo plano
    # (31 ago, pedido de Braulio: la página de Ubicación GPS se actualiza
    # sola cada 2 minutos). 0 desactiva la sincronización automática (queda
    # solo el botón manual "Sincronizar") — ver app/scheduler.py.
    FROTCOM_AUTO_SYNC_SECONDS = int(os.environ.get("FROTCOM_AUTO_SYNC_SECONDS", "120"))

    # Facturación electrónica SUNAT vía un OSE (NubeFacT, Efact, etc.). Ver
    # README, sección "Facturación electrónica (SUNAT)".
    OSE_RUTA = os.environ.get("OSE_RUTA", "")
    OSE_TOKEN = os.environ.get("OSE_TOKEN", "")
    COMPANY_RUC = os.environ.get("COMPANY_RUC", "")
    COMPANY_ADDRESS = os.environ.get("COMPANY_ADDRESS", "")
    INVOICE_SERIES = os.environ.get("INVOICE_SERIES", "F001")
    WAYBILL_SERIES = os.environ.get("WAYBILL_SERIES", "T001")

    # decolecta.com: tipo de cambio SUNAT (liquidación de Gastos) y
    # consulta de RUC (autocompletar proveedor al registrar un gasto).
    # Ambos servicios comparten el mismo token (DECOLECTA_TOKEN) — decisión
    # explícita de Braulio para no pedir una cuenta aparte. Ver README,
    # sección "Liquidaciones", y app/integrations/sunat_exchange_rate.py /
    # app/integrations/sunat_ruc.py.
    DECOLECTA_BASE_URL = os.environ.get("DECOLECTA_BASE_URL", "")
    DECOLECTA_RUC_BASE_URL = os.environ.get("DECOLECTA_RUC_BASE_URL", "")
    DECOLECTA_TOKEN = os.environ.get("DECOLECTA_TOKEN", "")
