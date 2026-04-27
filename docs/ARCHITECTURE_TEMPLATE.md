# Project Architecture

> **Instructions**: Fill in this template with your project's architecture.
> The scaffolder reads this document and uses it to generate your project structure.
> The more detail you provide, the better the output.

## Overview

<!-- One paragraph: what does this project do? Who is it for? -->

[Project Name] is a [type of application] that [core value proposition].
It is designed to be [key architectural qualities: self-hostable, scalable, etc.].

## Stack

<!-- List your technology choices -->

- **Backend**: Python 3.12 + FastAPI
- **Frontend**: React 18 + TypeScript
- **Database**: PostgreSQL 16
- **ORM**: SQLAlchemy 2.0 (async)
- **Cache**: Redis (optional)
- **Queue**: Celery + Redis (optional)
- **Auth**: JWT with refresh tokens
- **Deployment**: Docker Compose

## Architecture Layers

<!-- Define each layer of your application. Be specific about:
     - What lives in this layer
     - What directories it maps to
     - Rules/constraints that code in this layer must follow
     The scaffolder uses these rules for both generation and review. -->

### Layer 0 — Data Layer (`backend/db/`)

The foundational data persistence layer.

**Directories**:
- `backend/db/models/` — SQLAlchemy model definitions
- `backend/db/migrations/` — Alembic migration scripts
- `backend/db/session.py` — Database session factory

**Rules**:
- All schema changes go through Alembic migrations — never raw CREATE TABLE
- All tables must include `tenant_id` for multi-tenancy
- Use async sessions throughout
- Raw SQL is acceptable in migrations only — everywhere else use ORM

### Layer 1 — Services (`backend/services/`)

Business logic layer. All domain logic lives here — not in API routes, not in models.

**Directories**:
- `backend/services/` — One file per domain (user_service.py, task_service.py, etc.)

**Rules**:
- Services receive a DB session via constructor injection — never create their own
- Services never import from the API layer
- Services never call external APIs directly — use adapters
- Every public method should be async

### Layer 2 — API (`backend/api/`)

HTTP interface layer. Thin routes that delegate to services.

**Directories**:
- `backend/api/routes/` — FastAPI router files
- `backend/api/schemas/` — Pydantic v2 request/response models
- `backend/api/middleware/` — Auth, CORS, error handling middleware
- `backend/api/deps.py` — Dependency injection (get_db, get_current_user)

**Rules**:
- All endpoints must be async
- Routes should be thin — validate input, call service, return response
- Use Pydantic v2 models for all request/response — never return raw dicts
- Consistent error format: `{"error": "message", "code": "ERROR_CODE"}`
- Never expose raw exception tracebacks
- Auth middleware on all routes except health check

### Layer 3 — Frontend (`frontend/src/`)

React single-page application.

**Directories**:
- `frontend/src/components/` — Reusable UI components
- `frontend/src/pages/` — Route-level page components
- `frontend/src/api/` — API client functions
- `frontend/src/hooks/` — Custom React hooks
- `frontend/src/stores/` — State management (Zustand)
- `frontend/src/types/` — TypeScript type definitions

**Rules**:
- Functional components only — no class components
- API calls go through the api/ client module — never inline fetch()
- Type everything — no `any` types
- Core views in priority order: [View 1], [View 2], [View 3]

### Infrastructure (`infra/`)

**Directories**:
- `infra/docker/` — Dockerfiles
- `infra/nginx/` — Reverse proxy config

**Rules**:
- docker-compose.yml must include all required services
- All secrets via environment variables — never hardcoded

## Data Models

<!-- List your core domain models with their fields -->

### User
- `id`: UUID (primary key)
- `email`: string (unique)
- `name`: string
- `tenant_id`: UUID (foreign key)
- `role`: enum (admin, member, viewer)
- `created_at`: datetime
- `updated_at`: datetime

### Task
- `id`: UUID (primary key)
- `title`: string (required)
- `description`: text (optional)
- `status`: enum (todo, in_progress, review, done)
- `assignee_id`: UUID (foreign key → User, optional)
- `creator_id`: UUID (foreign key → User)
- `tenant_id`: UUID (foreign key)
- `due_date`: date (optional)
- `priority`: enum (low, medium, high, urgent)
- `created_at`: datetime
- `updated_at`: datetime

### Comment
- `id`: UUID (primary key)
- `task_id`: UUID (foreign key → Task)
- `author_id`: UUID (foreign key → User)
- `body`: text (required)
- `created_at`: datetime

## API Routes

<!-- List your API endpoints -->

| Method | Path | Purpose | Auth |
|--------|------|---------|------|
| POST | /api/v1/auth/login | Authenticate user | No |
| POST | /api/v1/auth/refresh | Refresh JWT token | No |
| GET | /api/v1/users/me | Get current user profile | Yes |
| GET | /api/v1/tasks | List tasks (filterable by status, assignee) | Yes |
| POST | /api/v1/tasks | Create a new task | Yes |
| GET | /api/v1/tasks/{id} | Get task detail | Yes |
| PUT | /api/v1/tasks/{id} | Update task | Yes |
| DELETE | /api/v1/tasks/{id} | Delete task | Yes (admin) |
| GET | /api/v1/tasks/{id}/comments | List comments on a task | Yes |
| POST | /api/v1/tasks/{id}/comments | Add comment to a task | Yes |

## Cross-Layer Contracts

<!-- Define the boundaries between layers — what can call what -->

- **API → Services**: Routes call service methods. Never call DB directly.
- **Services → DB**: Services use the session to query. Never call API layer.
- **Frontend → API**: Frontend calls REST endpoints only. Never accesses DB.
- **Services → External APIs**: Through adapter modules only. Never inline HTTP calls.

## Configuration

<!-- How is the app configured? -->

All configuration via environment variables loaded through a central `config.py`:
- `DATABASE_URL` — Postgres connection string
- `SECRET_KEY` — JWT signing key
- `REDIS_URL` — Redis connection (optional)
- `DEPLOYMENT_MODE` — dev | staging | production
- `LOG_LEVEL` — DEBUG | INFO | WARNING | ERROR

## Deployment

<!-- How should this be deployed? -->

- Primary: Docker Compose on a single host
- Database: PostgreSQL (external or in-compose)
- Reverse proxy: Nginx
- No cloud vendor lock-in
