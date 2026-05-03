INSERT INTO chunks(
    file_id,
    kind,
    symbol,
    package_name,
    start_line,
    end_line,
    content
)
VALUES (?, ?, ?, ?, ?, ?, ?);
