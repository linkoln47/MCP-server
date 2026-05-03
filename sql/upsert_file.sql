INSERT INTO files(path, language, hash, size_bytes, updated_at)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(path) DO UPDATE SET
    language = excluded.language,
    hash = excluded.hash,
    size_bytes = excluded.size_bytes,
    updated_at = excluded.updated_at
RETURNING id;
