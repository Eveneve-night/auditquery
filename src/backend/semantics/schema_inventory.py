"""Read-only SQLite schema inventory for domain-pack authoring.

No database values, inferred concepts or generated SQL. This inventory is not
an executable domain pack: sources, aliases, slot semantics and hashes still
need normal metadata review and DomainPackRegistry validation.
"""
from pathlib import Path
import sqlite3


def inspect_sqlite_schema(path: Path) -> dict:
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError('schema_source_not_file')
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        connection.execute('PRAGMA query_only=ON')
        tables = connection.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name LIMIT 1001").fetchall()
        if len(tables) > 1000:
            raise ValueError('schema_inventory_limit')
        inventory = []
        for (table,) in tables:
            columns = connection.execute('SELECT name, type, "notnull", pk FROM pragma_table_info(?)', (table,)).fetchall()
            if len(columns) > 4096:
                raise ValueError('schema_inventory_limit')
            foreign_keys = connection.execute('SELECT "table", "from", "to" FROM pragma_foreign_key_list(?)', (table,)).fetchall()
            inventory.append({'table': table,
                              'columns': [{'name': name, 'declared_type': declared_type,
                                           'not_null': bool(not_null), 'primary_key_order': pk}
                                          for name, declared_type, not_null, pk in columns],
                              'foreign_keys': [{'table': target, 'from': source, 'to': dest}
                                               for target, source, dest in foreign_keys]})
        return {'dialect': 'sqlite', 'scope': 'schema_only', 'tables': inventory,
                'business_rows_read': 0, 'execution_authorized': False}
    finally:
        connection.close()
