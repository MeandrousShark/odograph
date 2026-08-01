# Keep this import-light: tests import app.detector.core / .reconcile without
# needing psycopg or FastAPI installed. Runner (DB-coupled) is imported
# explicitly by the app.
