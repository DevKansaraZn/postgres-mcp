#!/usr/bin/env python
"""Entry point: `python server.py` (stdio MCP server).

Equivalent to `python -m pg_mcp`. Kept as a top-level file so MCP client
configs can point at one obvious path.
"""

from pg_mcp.server import main

if __name__ == "__main__":
    main()
