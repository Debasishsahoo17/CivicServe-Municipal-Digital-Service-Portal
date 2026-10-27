PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 name TEXT NOT NULL,
 email TEXT NOT NULL UNIQUE COLLATE NOCASE,
 password_hash TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('citizen','officer','admin')),
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS services (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 slug TEXT NOT NULL UNIQUE,
 title TEXT NOT NULL,
 category TEXT NOT NULL,
 description TEXT NOT NULL,
 icon TEXT NOT NULL,
 eta_days INTEGER NOT NULL,
 requirements TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS applications (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 reference TEXT NOT NULL UNIQUE,
 citizen_id INTEGER NOT NULL REFERENCES users(id),
 service_id INTEGER NOT NULL REFERENCES services(id),
 officer_id INTEGER REFERENCES users(id),
 status TEXT NOT NULL CHECK(status IN ('submitted','assigned','in_review','needs_info','approved','rejected')),
 subject TEXT NOT NULL,
 description TEXT NOT NULL,
 address TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 closed_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_cases_owner ON applications(citizen_id,created_at);
CREATE INDEX IF NOT EXISTS ix_cases_officer ON applications(officer_id,status);
CREATE INDEX IF NOT EXISTS ix_cases_status ON applications(status);
CREATE TABLE IF NOT EXISTS case_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
 actor_id INTEGER REFERENCES users(id),
 event_type TEXT NOT NULL,
 note TEXT NOT NULL DEFAULT '',
 from_status TEXT,
 to_status TEXT,
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS ix_events_case ON case_events(application_id,id);
CREATE TABLE IF NOT EXISTS notifications (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 application_id INTEGER REFERENCES applications(id) ON DELETE CASCADE,
 message TEXT NOT NULL,
 is_read INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS ix_notifications_user ON notifications(user_id,is_read,id);
CREATE TABLE IF NOT EXISTS audit_logs (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 actor_id INTEGER REFERENCES users(id),
 application_id INTEGER REFERENCES applications(id),
 action TEXT NOT NULL,
 details TEXT NOT NULL DEFAULT '',
 ip_address TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS ix_audit_created ON audit_logs(id DESC);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY,
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sessions_expires ON sessions(expires_at);
