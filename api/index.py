"""Vercel Python Functions entrypoint: re-exports the FastAPI app so vercel.json can
route /api/* here without duplicating backend_api.py's routes under an /api/ prefix."""
from backend_api import app  # noqa: F401
