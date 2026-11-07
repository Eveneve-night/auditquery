CREATE TABLE asset(id INTEGER PRIMARY KEY, asset_name TEXT NOT NULL, site TEXT NOT NULL);
CREATE TABLE work_order(id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL REFERENCES asset(id), status TEXT NOT NULL, planned_hours NUMERIC NOT NULL);
CREATE TABLE maintenance_event(id INTEGER PRIMARY KEY, work_order_id INTEGER NOT NULL REFERENCES work_order(id), duration_hours NUMERIC NOT NULL, event_date TEXT NOT NULL);
