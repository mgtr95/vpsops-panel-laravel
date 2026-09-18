from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pathlib import Path
from pydantic import BaseModel

from app.auth import AuthUser, require_action, require_priv
from app.services import db_svc, discover_svc

router = APIRouter(
    prefix="/api/db",
    tags=["db"],
    dependencies=[Depends(require_priv("db"))],
)


@router.get("/connections")
def connections(_user: AuthUser):
    try:
        discover_svc.sync()
    except Exception:
        pass
    return db_svc.list_connections()


@router.get("/{conn_id}/databases")
async def databases(conn_id: str, _user: AuthUser):
    try:
        return await db_svc.list_databases(conn_id)
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@router.get("/{conn_id}/tables")
async def tables(
    conn_id: str,
    _user: AuthUser,
    database: str | None = None,
):
    try:
        return await db_svc.list_tables(conn_id, database)
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@router.get("/{conn_id}/tables/{table}/columns")
async def columns(
    conn_id: str,
    table: str,
    _user: AuthUser,
    database: str | None = None,
):
    try:
        return await db_svc.table_columns(conn_id, table, database)
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@router.get("/{conn_id}/tables/{table}/rows")
async def rows(
    conn_id: str,
    table: str,
    _user: AuthUser,
    database: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    q: str | None = None,
    sort: str | None = None,
    order: str = Query("asc"),
    filters: str | None = None,
    match: str = Query("all"),
):
    parsed: list[dict] = []
    if filters:
        try:
            loaded = json.loads(filters)
        except json.JSONDecodeError as e:
            raise HTTPException(400, "Invalid filters JSON") from e
        if not isinstance(loaded, list):
            raise HTTPException(400, "filters must be a JSON array")
        parsed = loaded
    try:
        return await db_svc.browse_table(
            conn_id,
            table,
            database,
            limit,
            offset,
            q=q,
            sort=sort,
            order=order,
            filters=parsed,
            match=match,
        )
    except Exception as e:
        raise HTTPException(400, str(e)) from e


class SqlBody(BaseModel):
    sql: str
    database: str | None = None


@router.post("/{conn_id}/sql", dependencies=[Depends(require_action("db", "sql"))])
async def sql(conn_id: str, body: SqlBody, _user: AuthUser):
    try:
        return await db_svc.run_sql(conn_id, body.sql, body.database)
    except Exception as e:
        raise HTTPException(400, str(e)) from e


class RowBody(BaseModel):
    pk: dict[str, Any] = {}
    values: dict[str, Any] = {}
    database: str | None = None


@router.put("/{conn_id}/tables/{table}/row", dependencies=[Depends(require_action("db", "update"))])
async def update_row(
    conn_id: str, table: str, body: RowBody, _user: AuthUser
):
    try:
        return await db_svc.update_row(
            conn_id, table, body.pk, body.values, body.database
        )
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@router.post("/{conn_id}/tables/{table}/row", dependencies=[Depends(require_action("db", "insert"))])
async def insert_row(
    conn_id: str, table: str, body: RowBody, _user: AuthUser
):
    try:
        return await db_svc.insert_row(conn_id, table, body.values, body.database)
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/{conn_id}/tables/{table}/row", dependencies=[Depends(require_action("db", "delete_row"))])
async def delete_row(
    conn_id: str, table: str, body: RowBody, _user: AuthUser
):
    try:
        return await db_svc.delete_row(conn_id, table, body.pk, body.database)
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@router.get("/{conn_id}/dump", dependencies=[Depends(require_action("db", "dump"))])
async def dump(
    conn_id: str,
    _user: AuthUser,
    database: str | None = None,
):
    try:
        data = await db_svc.dump_database(conn_id, database)
        cfg = db_svc.get_db_config(conn_id)
        if cfg.get("engine") == "sqlite":
            filename = Path(cfg.get("path") or "database.sqlite").name
            media = "application/octet-stream"
        else:
            filename = f"{database or cfg.get('database') or 'dump'}.sql"
            media = "application/sql"
        return Response(
            content=data,
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@router.post("/{conn_id}/import", dependencies=[Depends(require_action("db", "import"))])
async def import_sql(
    conn_id: str,
    _user: AuthUser,
    file: UploadFile = File(...),
    database: str | None = None,
):
    try:
        content = await file.read()
        return await db_svc.import_sql(conn_id, content, database)
    except Exception as e:
        raise HTTPException(400, str(e)) from e
