INSERT INTO files(path, language, is_test, hash, size_bytes, updated_at)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(path) DO UPDATE SET
    language = excluded.language,
    is_test = excluded.is_test,
    hash = excluded.hash,
    size_bytes = excluded.size_bytes,
    updated_at = excluded.updated_at
RETURNING id;
