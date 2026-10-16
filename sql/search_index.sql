SELECT
    chunks.id,
    files.path,
    files.is_test,
    chunks.kind,
    chunks.symbol,
    chunks.package_name,
    chunks.start_line,
    chunks.end_line,
    snippet(chunks_fts, 2, '[', ']', '...', 12) AS snippet,
    bm25(chunks_fts) AS score
FROM chunks_fts
JOIN chunks ON chunks_fts.rowid = chunks.id
JOIN files ON chunks.file_id = files.id
WHERE chunks_fts MATCH ?
  AND (? OR files.is_test = 0)
ORDER BY score
LIMIT ?;
