FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn[standard] sqlalchemy[asyncio] asyncpg pgvector pydantic pydantic-settings python-dotenv redis httpx aiosqlite pyyaml "mcp==1.29.0"

COPY gateway/ gateway/
COPY core/ core/
COPY scanner/ scanner/
COPY mcp/ mcp/

EXPOSE 8000
EXPOSE 8001

CMD ["python", "mcp/server.py", "--transport", "stdio"]
