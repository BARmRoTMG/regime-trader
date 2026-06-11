"""FastAPI dependency providers shared across all route modules."""

from db.database import Database, get_db


def get_database() -> Database:
    """FastAPI dependency: returns the singleton Database instance."""
    return get_db()
