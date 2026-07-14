# HCC — Hanyan Cognitive Core

## Phase 1 开发环境

```bash
# 安装依赖
uv sync

# 复制环境变量
cp .env.example .env

# 启动数据库
docker compose up -d db

# 运行迁移
alembic upgrade head

# 启动服务
uv run uvicorn gateway.main:app --reload
```

## API 文档

启动后访问 http://localhost:8000/docs
