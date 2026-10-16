SELECT
    chunks.id,
    files.path,
    files.language,
    files.is_test,
    files.hash,
    chunks.kind,
    chunks.symbol,
    chunks.package_name,
    chunks.start_line,
    chunks.end_line,
    chunks.content
FROM chunks
JOIN files ON chunks.file_id = files.id
WHERE chunks.id = ?;
