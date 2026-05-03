from mcp.server.fastmcp import FastMCP

mcp = FastMCP("repo-search", "A tool to search and map repository files.")


@mcp.tool()
def search_code(query: str, limit: int = 10) -> str:
    """
    Search indexed project code before reading files directly.
    """
    # results = search.search_code(query, limit)
    return f"Search results for: {query}"


@mcp.tool()
def get_chunk(chunk_id: str) -> str:
    """
    Return a full indexed code chunk by id.
    """
    # chunk = db.get_chunk(chunk_id)
    return f"Chunk: {chunk_id}"


@mcp.tool()
def lookup_symbol(name: str) -> str:
    """
    Find Go functions, methods, structs, interfaces, constants, and variables.
    """
    # results = search.lookup_symbol(name)
    return f"Symbol search: {name}"


@mcp.tool()
def repo_map() -> str:
    """
    Return compact repository map.
    """
    return "backend/, frontend/, internal/, cmd/..."

if __name__ == "__main__":
    mcp.run()