-- Cache database: data/cache.db, attached to every connection as `cache`.
-- Safe to delete at any time: entries are refetched when needed.
-- Strip the API key from `url` before storing it. Never store IEEE responses here.
CREATE TABLE IF NOT EXISTS cache.raw_response (
    key        TEXT PRIMARY KEY,
    url        TEXT NOT NULL,
    body       BLOB NOT NULL,  -- zlib-compressed JSON
    fetched_at TEXT NOT NULL
);
