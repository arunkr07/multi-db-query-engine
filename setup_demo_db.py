"""Create a local SQLite demo database for the SDK examples."""
import sqlite3
from pathlib import Path

DB_PATH = Path("demo.db")

with sqlite3.connect(DB_PATH) as connection:
    connection.execute("CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, customer_name TEXT NOT NULL, product TEXT NOT NULL, amount REAL NOT NULL, status TEXT NOT NULL)")
    connection.execute("DELETE FROM orders")
    connection.executemany("INSERT INTO orders (id, customer_name, product, amount, status) VALUES (?, ?, ?, ?, ?)", [
        (1, "Aarav", "Keyboard", 2500.0, "paid"),
        (2, "Priya", "Mouse", 800.0, "paid"),
        (3, "Rahul", "Monitor", 12000.0, "pending"),
        (4, "Ananya", "Headphones", 3500.0, "paid"),
        (5, "Rohan", "Webcam", 2200.0, "pending"),
    ])
print(f"Demo database ready: {DB_PATH.resolve()}")
