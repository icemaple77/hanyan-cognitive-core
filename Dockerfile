FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn[standard] sqlalchemy[asyncio] asyncpg pgvector pydantic pydantic-settings python-dotenv redis httpx aiosqlite pyyaml

COPY gateway/ gateway/
COPY core/ core/
COPY scanner/ scanner/
COPY mcp/ mcp/

EXPOSE 8000

CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
