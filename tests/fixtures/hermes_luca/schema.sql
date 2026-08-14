PRAGMA foreign_keys = ON;

CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    chat_type TEXT,
    user_id TEXT,
    session_key TEXT
);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    content TEXT,
    timestamp INTEGER NOT NULL,
    role TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE INDEX messages_session_timestamp_idx
    ON messages(session_id, timestamp, id);
