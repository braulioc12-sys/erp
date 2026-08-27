"""Carga datos de ejemplo: usuarios, clientes, flota, conductores y viajes.

Uso:
    python seed.py
"""
from app import create_app
from app.seed_data import seed_demo_data


def main():
    app = create_app()
    with app.app_context():
        seed_demo_data()


if __name__ == "__main__":
    main()
