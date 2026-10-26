"""Print configured database names and discovered schema without exposing credentials."""
from multi_db_query_engine import MultiDBQueryEngine

with MultiDBQueryEngine() as engine:
    print("Databases:", engine.databases)
    print("Schemas:", engine.schemas)
