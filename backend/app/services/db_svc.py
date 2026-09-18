from __future__ import annotations

import json
import os
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiomysql
import psycopg
import sqlite3

from app.config import get_settings
from app.utils import parse_dotenv, read_json

MYSQL_SYSTEM_SCHEMAS = {
    "information_schema",
    "performance_schema",
    "mysql",
    "sys",
}
POSTGRES_TEMPLATE_DBS = {"template0", "template1"}
GENERIC_SQL_HOSTS = {
    "127.0.0.1",
    "localhost",
    "::1",
    "mysql",
    "mariadb",
    "postgres",
    "postgresql",
    "pgsql",
    "db",
    "database",
}
DEFAULT_ENV_MAP = {
    "host": "DB_HOST",
    "port": "DB_PORT",
    "user": "DB_USERNAME",
    "password": "DB_PASSWORD",
    "db": "DB_DATABASE",
}

FILTER_OPS = {
    "contains",
    "equals",
    "not_equals",
    "starts_with",
    "ends_with",
    "gt",
    "gte",
    "lt",
    "lte",
    "empty",
    "not_empty",
}


def load_database_configs() -> list[dict]:
    settings = get_settings()
    configs = read_json(settings.databases_config_path, None)
    if configs is None:
        configs = read_json(settings.config_path, {}).get("databases")
    return configs or []


def get_db_config(conn_id: str) -> dict:
    for cfg in load_database_configs():
        if cfg.get("id") == conn_id:
            return cfg
    raise ValueError(f"Unknown connection: {conn_id}")


def _normalize_engine(cfg: dict) -> str:
    engine = (cfg.get("engine") or "mysql").lower()
    if engine in ("postgres", "postgresql", "pgsql"):
        return "postgres"
    return engine


def _is_mysql(cfg: dict) -> bool:
    return _normalize_engine(cfg) == "mysql"


def _is_postgres(cfg: dict) -> bool:
    return _normalize_engine(cfg) == "postgres"


def _is_sql_server(cfg: dict) -> bool:
    return _is_mysql(cfg) or _is_postgres(cfg)


def _resolve_sql_server(cfg: dict, *, engine: str, default_port: int) -> dict[str, Any]:
    default_user = "root" if engine == "mysql" else "postgres"
    resolved = {
        "id": cfg["id"],
        "name": cfg.get("name", cfg["id"]),
        "engine": engine,
        "host": cfg.get("host", "localhost"),
        "port": int(cfg.get("port", default_port)),
        "user": cfg.get("user", default_user),
        "password": cfg.get("password", ""),
        "db": cfg.get("database", cfg.get("db", "")),
    }

    env_file = cfg.get("env_file")
    env_map = cfg.get("env_map") or DEFAULT_ENV_MAP
    if env_file:
        env = parse_dotenv(env_file)
        for key, env_key in env_map.items():
            if env.get(env_key):
                resolved[key] = env[env_key]
        if "port" in resolved:
            resolved["port"] = int(resolved["port"])

    pw_env = cfg.get("password_env")
    if pw_env and os.environ.get(pw_env):
        resolved["password"] = os.environ[pw_env]

    host = str(resolved.get("host") or "")
    stored = str(cfg.get("host") or "")
    if host.lower() in GENERIC_SQL_HOSTS and stored and stored.lower() not in GENERIC_SQL_HOSTS:
        resolved["host"] = stored
        host = stored
    panel_host = _panel_db_host_for_cfg(cfg)
    if panel_host and host != panel_host and not _peer_running(host) and _peer_running(panel_host):
        resolved["host"] = panel_host
    return resolved


def _peer_running(host: str) -> bool:
    name = (host or "").strip()
    if not name or name.lower() in GENERIC_SQL_HOSTS:
        return False
    try:
        from app.services import docker_svc

        return docker_svc.get_container(name).status == "running"
    except Exception:
        return False


def _panel_db_host_for_cfg(cfg: dict) -> str | None:
    try:
        from app.services import deploy_svc, laravel_svc
    except Exception:
        return None
    cid = str(cfg.get("id") or "")
    env_file = str(cfg.get("env_file") or "")
    for app in deploy_svc.list_apps():
        if not app.get("managed"):
            continue
        slug = str(app.get("id") or "")
        if not slug:
            continue
        belongs = cid in {laravel_svc.panel_db_id(slug), f"{slug}-postgres", f"{slug}-mysql"}
        path = str(app.get("path") or "")
        if not belongs and env_file and path:
            belongs = laravel_svc.env_file_belongs_to_app(env_file, path)
        if not belongs:
            continue
        entry = laravel_svc.databases_entry(app)
        host = str((entry or {}).get("host") or "")
        return host or None
    return None


def _resolve_mysql(cfg: dict) -> dict[str, Any]:
    return _resolve_sql_server(cfg, engine="mysql", default_port=3306)


def _resolve_postgres(cfg: dict) -> dict[str, Any]:
    return _resolve_sql_server(cfg, engine="postgres", default_port=5432)


def _ensure_peer(host: str) -> None:
    try:
        from app.services import proxy_svc

        proxy_svc.connect_dashboard_to_peer(host)
    except Exception:
        pass


def list_connections() -> list[dict]:
    out = []
    for cfg in load_database_configs():
        engine = _normalize_engine(cfg)
        if engine == "mysql":
            m = _resolve_mysql(cfg)
            _ensure_peer(m["host"])
            out.append(
                {
                    "id": m["id"],
                    "name": m["name"],
                    "engine": "mysql",
                    "host": m["host"],
                    "database": m["db"],
                }
            )
        elif engine == "postgres":
            p = _resolve_postgres(cfg)
            _ensure_peer(p["host"])
            out.append(
                {
                    "id": p["id"],
                    "name": p["name"],
                    "engine": "postgres",
                    "host": p["host"],
                    "database": p["db"],
                }
            )
        elif engine == "sqlite":
            path = cfg.get("path", "")
            out.append(
                {
                    "id": cfg["id"],
                    "name": cfg.get("name", cfg["id"]),
                    "engine": "sqlite",
                    "path": path,
                    "exists": Path(path).exists() if path else False,
                }
            )
    return out


def _is_unknown_database_error(exc: BaseException) -> bool:
    sqlstate = str(getattr(exc, "sqlstate", "") or "")
    msg = str(exc).lower()
    if sqlstate == "3D000":
        return True
    return "does not exist" in msg or "unknown database" in msg


async def _mysql_pool(cfg: dict, database: str | None = None):
    m = _resolve_mysql(cfg)
    _ensure_peer(m["host"])
    wanted = (database or m["db"] or None) or None
    fallback = m["db"] or None

    async def open_pool(db: str | None):
        return await aiomysql.create_pool(
            host=m["host"],
            port=m["port"],
            user=m["user"],
            password=m["password"],
            db=db,
            autocommit=True,
            minsize=1,
            maxsize=3,
        )

    try:
        return await open_pool(wanted)
    except Exception as e:
        if wanted and fallback and wanted != fallback and _is_unknown_database_error(e):
            return await open_pool(fallback)
        raise


async def _postgres_conn(cfg: dict, database: str | None = None):
    p = _resolve_postgres(cfg)
    _ensure_peer(p["host"])
    wanted = (database or p["db"] or "postgres").strip() or "postgres"
    fallback = (p["db"] or "postgres").strip() or "postgres"

    async def open_conn(dbname: str):
        return await psycopg.AsyncConnection.connect(
            host=p["host"],
            port=p["port"],
            user=p["user"],
            password=p["password"],
            dbname=dbname,
            autocommit=True,
            sslmode="disable",
        )

    try:
        return await open_conn(wanted)
    except psycopg.Error as e:
        if wanted != fallback and _is_unknown_database_error(e):
            return await open_conn(fallback)
        raise


def _sqlite_conn(cfg: dict):
    path = cfg.get("path", "")
    if not path or not Path(path).exists():
        raise FileNotFoundError(f"SQLite DB not found: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _quote_mysql(name: str) -> str:
    return "`" + str(name).replace("`", "``") + "`"


def _quote_postgres(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _quote_sqlite(name: str) -> str:
    return "[" + str(name).replace("]", "]]") + "]"


def _quote(cfg: dict, name: str) -> str:
    if _is_mysql(cfg):
        return _quote_mysql(name)
    if _is_postgres(cfg):
        return _quote_postgres(name)
    return _quote_sqlite(name)


def _table_name(row: dict) -> str:
    return str(row.get("Name") or row.get("name") or "")


def _col_name(col: dict) -> str:
    return str(col.get("Field") or col.get("name") or "")


def _is_binary_type(type_str: str | None) -> bool:
    t = (type_str or "").lower()
    return any(x in t for x in ("blob", "binary", "varbinary", "bytea"))


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like_escape_clause(cfg: dict) -> str:
    # MySQL and Postgres treat \ as a string escape in this clause.
    return "ESCAPE '\\\\'" if _is_sql_server(cfg) else "ESCAPE '\\'"


def _ph(cfg: dict) -> str:
    return "%s" if _is_sql_server(cfg) else "?"


def _cast_text(quoted: str, cfg: dict) -> str:
    return f"CAST({quoted} AS CHAR)" if _is_mysql(cfg) else f"CAST({quoted} AS TEXT)"


async def _resolve_table(
    conn_id: str, table: str, database: str | None = None
) -> str:
    tables = await list_tables(conn_id, database)
    names = [_table_name(t) for t in tables if _table_name(t)]
    if table not in names:
        raise ValueError(f"Unknown table: {table}")
    return table


async def _allowed_columns(
    conn_id: str, table: str, database: str | None = None
) -> list[dict]:
    cols = await table_columns(conn_id, table, database)
    return [c for c in cols if _col_name(c)]


def _filter_predicate(
    quoted: str, op: str, value: Any, cfg: dict, ph: str
) -> tuple[str, list[Any]]:
    text = _cast_text(quoted, cfg)
    if op == "empty":
        return f"({quoted} IS NULL OR {text} = '')", []
    if op == "not_empty":
        return f"({quoted} IS NOT NULL AND {text} != '')", []
    if value is None:
        raise ValueError(f"value required for operator {op}")
    esc = _like_escape_clause(cfg)
    raw = "" if value is None else str(value)
    if op == "contains":
        return f"{text} LIKE {ph} {esc}", [f"%{_like_escape(raw)}%"]
    if op == "starts_with":
        return f"{text} LIKE {ph} {esc}", [f"{_like_escape(raw)}%"]
    if op == "ends_with":
        return f"{text} LIKE {ph} {esc}", [f"%{_like_escape(raw)}"]
    if op == "equals":
        return f"{quoted} = {ph}", [value]
    if op == "not_equals":
        return f"{quoted} <> {ph}", [value]
    if op == "gt":
        return f"{quoted} > {ph}", [value]
    if op == "gte":
        return f"{quoted} >= {ph}", [value]
    if op == "lt":
        return f"{quoted} < {ph}", [value]
    if op == "lte":
        return f"{quoted} <= {ph}", [value]
    raise ValueError(f"Unknown operator: {op}")


def _build_where(
    cfg: dict,
    columns: list[dict],
    q: str | None,
    filters: list[dict] | None,
    match: str,
) -> tuple[str, list[Any]]:
    ph = _ph(cfg)
    allowed = {_col_name(c): c for c in columns}
    clauses: list[str] = []
    params: list[Any] = []

    q = (q or "").strip()
    if q:
        search_cols = [
            c
            for c in columns
            if _col_name(c) and not _is_binary_type(c.get("Type"))
        ]
        if search_cols:
            pattern = f"%{_like_escape(q)}%"
            parts = []
            for c in search_cols:
                quoted = _quote(cfg, _col_name(c))
                text = _cast_text(quoted, cfg)
                parts.append(f"{text} LIKE {ph} {_like_escape_clause(cfg)}")
                params.append(pattern)
            clauses.append("(" + " OR ".join(parts) + ")")

    rule_parts: list[str] = []
    rule_params: list[Any] = []
    for rule in filters or []:
        if not isinstance(rule, dict):
            raise ValueError("Each filter must be an object")
        col = str(rule.get("column") or "")
        op = str(rule.get("op") or "").strip().lower()
        if op not in FILTER_OPS:
            raise ValueError(f"Unknown operator: {op or '(empty)'}")
        if col not in allowed:
            raise ValueError(f"Unknown column: {col}")
        if op not in ("empty", "not_empty") and rule.get("value") in (None, ""):
            continue
        quoted = _quote(cfg, col)
        sql, bind = _filter_predicate(quoted, op, rule.get("value"), cfg, ph)
        rule_parts.append(sql)
        rule_params.extend(bind)

    if rule_parts:
        joiner = " OR " if match == "any" else " AND "
        clauses.append("(" + joiner.join(rule_parts) + ")")
        params.extend(rule_params)

    if not clauses:
        return "", []
    return " WHERE " + " AND ".join(clauses), params


_mysql_ssl_cli_args: list[str] | None = None


def _mysql_cli_ssl_args() -> list[str]:
    """Flags so mysql/mysqldump accept Docker MySQL 8's default self-signed TLS cert.

    Debian's default-mysql-client is MariaDB. Recent MariaDB clients verify
    the server certificate by default, which fails against the self-signed
    cert MySQL 8 containers generate.
    """
    global _mysql_ssl_cli_args
    if _mysql_ssl_cli_args is not None:
        return _mysql_ssl_cli_args
    import subprocess

    try:
        help_text = subprocess.run(
            ["mysqldump", "--help"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        ).stdout.lower()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        _mysql_ssl_cli_args = []
        return _mysql_ssl_cli_args

    if "ssl-verify-server-cert" in help_text:
        _mysql_ssl_cli_args = ["--skip-ssl-verify-server-cert"]
    elif "ssl-mode" in help_text:
        _mysql_ssl_cli_args = ["--ssl-mode=DISABLED"]
    else:
        _mysql_ssl_cli_args = ["--ssl=0"]
    return _mysql_ssl_cli_args


def _mysql_cli_env() -> dict[str, str]:
    env = os.environ.copy()
    # MariaDB Connector/C 3.4+ verifies TLS peers even when some CLI flags are set.
    env["MARIADB_TLS_DISABLE_PEER_VERIFICATION"] = "1"
    return env


def _pg_cli_env(password: str) -> dict[str, str]:
    env = os.environ.copy()
    env["PGPASSWORD"] = password
    env["PGSSLMODE"] = "disable"
    return env


async def _server_execute(
    cfg: dict,
    database: str | None,
    sql: str,
    params: list[Any] | tuple[Any, ...] | None = None,
) -> tuple[list[str], list[Any], int]:
    bind = list(params or [])
    if _is_mysql(cfg):
        pool = await _mysql_pool(cfg, database)
        try:
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(sql, bind)
                    cols = [d[0] for d in cur.description] if cur.description else []
                    rows = await cur.fetchall() if cur.description else []
                    return cols, rows, cur.rowcount
        finally:
            pool.close()
            await pool.wait_closed()
    conn = await _postgres_conn(cfg, database)
    try:
        async with conn.cursor() as cur:
            if bind:
                await cur.execute(sql, bind)
            else:
                await cur.execute(sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = list(await cur.fetchall()) if cur.description else []
            return cols, rows, cur.rowcount
    finally:
        await conn.close()


async def list_databases(conn_id: str) -> list[str]:
    cfg = get_db_config(conn_id)
    if _is_mysql(cfg):
        _cols, rows, _n = await _server_execute(cfg, None, "SHOW DATABASES")
        return [r[0] for r in rows if r[0] not in MYSQL_SYSTEM_SCHEMAS]
    if _is_postgres(cfg):
        p = _resolve_postgres(cfg)
        try:
            _cols, rows, _n = await _server_execute(
                cfg,
                "postgres",
                "SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY datname",
            )
        except Exception:
            return [p["db"]] if p.get("db") else []
        names = [r[0] for r in rows if r[0] not in POSTGRES_TEMPLATE_DBS]
        app_dbs = [n for n in names if n != "postgres"]
        return app_dbs or names or ([p["db"]] if p.get("db") else [])
    return ["main"]


async def list_tables(conn_id: str, database: str | None = None) -> list[dict]:
    cfg = get_db_config(conn_id)
    if _is_mysql(cfg):
        pool = await _mysql_pool(cfg, database)
        try:
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SHOW TABLE STATUS")
                    cols = [d[0] for d in cur.description]
                    rows = await cur.fetchall()
                    return [dict(zip(cols, r)) for r in rows]
        finally:
            pool.close()
            await pool.wait_closed()
    if _is_postgres(cfg):
        _cols, rows, _n = await _server_execute(
            cfg,
            database,
            """
            SELECT c.relname AS "Name",
                   CASE c.relkind
                     WHEN 'r' THEN 'table'
                     WHEN 'p' THEN 'table'
                     WHEN 'v' THEN 'view'
                     WHEN 'm' THEN 'materialized view'
                     ELSE c.relkind::text
                   END AS "Comment",
                   GREATEST(c.reltuples::bigint, 0) AS "Rows"
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relkind IN ('r', 'p', 'v', 'm')
            ORDER BY c.relname
            """,
        )
        return [{"Name": r[0], "Comment": r[1], "Rows": r[2]} for r in rows]
    conn = _sqlite_conn(cfg)
    try:
        cur = conn.execute(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
        )
        return [
            {"Name": r["name"], "Comment": r["type"]}
            for r in cur.fetchall()
            if r["name"] and not str(r["name"]).startswith("sqlite_")
        ]
    finally:
        conn.close()


async def table_columns(
    conn_id: str, table: str, database: str | None = None
) -> list[dict]:
    cfg = get_db_config(conn_id)
    table = await _resolve_table(conn_id, table, database)
    if _is_mysql(cfg):
        pool = await _mysql_pool(cfg, database)
        try:
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(f"SHOW COLUMNS FROM {_quote_mysql(table)}")
                    cols = [d[0] for d in cur.description]
                    rows = await cur.fetchall()
                    return [dict(zip(cols, r)) for r in rows]
        finally:
            pool.close()
            await pool.wait_closed()
    if _is_postgres(cfg):
        _cols, rows, _n = await _server_execute(
            cfg,
            database,
            """
            SELECT a.attname AS "Field",
                   pg_catalog.format_type(a.atttypid, a.atttypmod) AS "Type",
                   CASE WHEN a.attnotnull THEN 'NO' ELSE 'YES' END AS "Null",
                   CASE WHEN EXISTS (
                       SELECT 1 FROM pg_index i
                       WHERE i.indrelid = a.attrelid
                         AND i.indisprimary
                         AND a.attnum = ANY (i.indkey)
                   ) THEN 'PRI' ELSE '' END AS "Key",
                   pg_get_expr(ad.adbin, ad.adrelid) AS "Default",
                   CASE
                     WHEN a.attidentity IN ('a', 'd') THEN 'auto_increment'
                     WHEN strpos(coalesce(pg_get_expr(ad.adbin, ad.adrelid), ''), 'nextval(') = 1
                       THEN 'auto_increment'
                     ELSE ''
                   END AS "Extra"
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
            WHERE n.nspname = 'public'
              AND c.relname = %s
              AND a.attnum > 0
              AND NOT a.attisdropped
            ORDER BY a.attnum
            """,
            [table],
        )
        return [
            {
                "Field": r[0],
                "Type": r[1],
                "Null": r[2],
                "Key": r[3],
                "Default": r[4],
                "Extra": r[5] or "",
            }
            for r in rows
        ]
    conn = _sqlite_conn(cfg)
    try:
        cur = conn.execute(f"PRAGMA table_info({_quote_sqlite(table)})")
        return [
            {
                "Field": r["name"],
                "Type": r["type"],
                "Null": "YES" if not r["notnull"] else "NO",
                "Key": "PRI" if r["pk"] else "",
                "Default": r["dflt_value"],
                "Extra": "auto_increment"
                if r["pk"] and str(r["type"] or "").upper() == "INTEGER"
                else "",
            }
            for r in cur.fetchall()
        ]
    finally:
        conn.close()


async def browse_table(
    conn_id: str,
    table: str,
    database: str | None = None,
    limit: int = 100,
    offset: int = 0,
    q: str | None = None,
    sort: str | None = None,
    order: str = "asc",
    filters: list[dict] | None = None,
    match: str = "all",
) -> dict:
    cfg = get_db_config(conn_id)
    table = await _resolve_table(conn_id, table, database)
    columns_meta = await _allowed_columns(conn_id, table, database)
    allowed_names = {_col_name(c) for c in columns_meta}

    match = (match or "all").strip().lower()
    if match not in ("all", "any"):
        raise ValueError("match must be 'all' or 'any'")
    order_sql = "DESC" if str(order).lower() == "desc" else "ASC"

    where_sql, where_params = _build_where(cfg, columns_meta, q, filters, match)
    quoted_table = _quote(cfg, table)
    order_sql_clause = ""
    if sort:
        if sort not in allowed_names:
            raise ValueError(f"Unknown sort column: {sort}")
        order_sql_clause = f" ORDER BY {_quote(cfg, sort)} {order_sql}"

    ph = _ph(cfg)
    count_sql = f"SELECT COUNT(*) FROM {quoted_table}{where_sql}"
    data_sql = (
        f"SELECT * FROM {quoted_table}{where_sql}{order_sql_clause} "
        f"LIMIT {ph} OFFSET {ph}"
    )
    data_params = [*where_params, limit, offset]

    if _is_sql_server(cfg):
        _cols, count_rows, _n = await _server_execute(
            cfg, database, count_sql, where_params
        )
        total = count_rows[0][0] if count_rows else 0
        cols, rows, _n = await _server_execute(cfg, database, data_sql, data_params)
        return {
            "columns": cols,
            "rows": [_serialize_row(r) for r in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    conn = _sqlite_conn(cfg)
    try:
        total = conn.execute(count_sql, where_params).fetchone()[0]
        cur = conn.execute(data_sql, data_params)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = [_serialize_row(r) for r in cur.fetchall()]
        return {
            "columns": cols,
            "rows": rows,
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    finally:
        conn.close()


def _serialize_row(row) -> list[Any]:
    return [_serialize_cell(cell) for cell in row]


def _serialize_cell(cell: Any) -> Any:
    if cell is None:
        return None
    if isinstance(cell, bool):
        return cell
    if isinstance(cell, (bytes, memoryview, bytearray)):
        return None
    if isinstance(cell, uuid.UUID):
        return str(cell)
    if isinstance(cell, Decimal):
        return str(cell)
    if isinstance(cell, (dict, list)):
        return json.dumps(cell, default=str)
    if hasattr(cell, "isoformat"):
        try:
            return cell.isoformat(sep=" ")
        except TypeError:
            return cell.isoformat()
    if isinstance(cell, (int, float, str)):
        return cell
    return str(cell)


async def run_sql(conn_id: str, sql: str, database: str | None = None) -> dict:
    sql = sql.strip()
    if not sql:
        raise ValueError("Empty SQL")
    cfg = get_db_config(conn_id)
    if _is_sql_server(cfg):
        cols, rows, rowcount = await _server_execute(cfg, database, sql)
        if cols:
            return {
                "columns": cols,
                "rows": [_serialize_row(r) for r in rows],
                "rowcount": rowcount,
            }
        return {"columns": [], "rows": [], "rowcount": rowcount}
    conn = _sqlite_conn(cfg)
    try:
        cur = conn.execute(sql)
        conn.commit()
        if cur.description:
            cols = [d[0] for d in cur.description]
            rows = [_serialize_row(r) for r in cur.fetchall()]
            return {"columns": cols, "rows": rows, "rowcount": cur.rowcount}
        return {"columns": [], "rows": [], "rowcount": cur.rowcount}
    finally:
        conn.close()


async def update_row(
    conn_id: str,
    table: str,
    pk: dict[str, Any],
    values: dict[str, Any],
    database: str | None = None,
) -> dict:
    if not pk or not values:
        raise ValueError("pk and values required")
    cfg = get_db_config(conn_id)
    table = await _resolve_table(conn_id, table, database)
    allowed = {_col_name(c) for c in await _allowed_columns(conn_id, table, database)}
    for key in list(pk) + list(values):
        if key not in allowed:
            raise ValueError(f"Unknown column: {key}")
    qt = _quote(cfg, table)
    ph = _ph(cfg)
    set_clause = ", ".join(f"{_quote(cfg, k)}={ph}" for k in values)
    where = " AND ".join(f"{_quote(cfg, k)}={ph}" for k in pk)
    sql = f"UPDATE {qt} SET {set_clause} WHERE {where}"
    params = list(values.values()) + list(pk.values())
    if _is_sql_server(cfg):
        _cols, _rows, rowcount = await _server_execute(cfg, database, sql, params)
        return {"rowcount": rowcount}
    conn = _sqlite_conn(cfg)
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return {"rowcount": cur.rowcount}
    finally:
        conn.close()


async def insert_row(
    conn_id: str,
    table: str,
    values: dict[str, Any],
    database: str | None = None,
) -> dict:
    if not values:
        raise ValueError("values required")
    cfg = get_db_config(conn_id)
    table = await _resolve_table(conn_id, table, database)
    allowed = {_col_name(c) for c in await _allowed_columns(conn_id, table, database)}
    for key in values:
        if key not in allowed:
            raise ValueError(f"Unknown column: {key}")
    cols = list(values.keys())
    ph = _ph(cfg)
    placeholders = ", ".join([ph] * len(cols))
    col_sql = ", ".join(_quote(cfg, c) for c in cols)
    sql = f"INSERT INTO {_quote(cfg, table)} ({col_sql}) VALUES ({placeholders})"
    if _is_sql_server(cfg):
        _cols, _rows, rowcount = await _server_execute(
            cfg, database, sql, list(values.values())
        )
        return {"rowcount": rowcount, "lastrowid": None}
    conn = _sqlite_conn(cfg)
    try:
        cur = conn.execute(sql, list(values.values()))
        conn.commit()
        return {"rowcount": cur.rowcount, "lastrowid": cur.lastrowid}
    finally:
        conn.close()


async def delete_row(
    conn_id: str,
    table: str,
    pk: dict[str, Any],
    database: str | None = None,
) -> dict:
    if not pk:
        raise ValueError("pk required")
    cfg = get_db_config(conn_id)
    table = await _resolve_table(conn_id, table, database)
    allowed = {_col_name(c) for c in await _allowed_columns(conn_id, table, database)}
    for key in pk:
        if key not in allowed:
            raise ValueError(f"Unknown column: {key}")
    ph = _ph(cfg)
    where = " AND ".join(f"{_quote(cfg, k)}={ph}" for k in pk)
    sql = f"DELETE FROM {_quote(cfg, table)} WHERE {where}"
    if _is_sql_server(cfg):
        _cols, _rows, rowcount = await _server_execute(
            cfg, database, sql, list(pk.values())
        )
        return {"rowcount": rowcount}
    conn = _sqlite_conn(cfg)
    try:
        cur = conn.execute(sql, list(pk.values()))
        conn.commit()
        return {"rowcount": cur.rowcount}
    finally:
        conn.close()


async def dump_from_cfg(cfg: dict, database: str | None = None) -> bytes:
    import asyncio

    if _is_mysql(cfg):
        m = _resolve_mysql(cfg)
        _ensure_peer(m["host"])
        db = database or m["db"]
        proc = await asyncio.create_subprocess_exec(
            "mysqldump",
            *_mysql_cli_ssl_args(),
            "--no-tablespaces",
            "--single-transaction",
            "--routines",
            "--triggers",
            "-h",
            m["host"],
            "-P",
            str(m["port"]),
            "-u",
            m["user"],
            f"-p{m['password']}",
            db,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_mysql_cli_env(),
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(stderr.decode(errors="replace"))
        return stdout
    if _is_postgres(cfg):
        p = _resolve_postgres(cfg)
        db = database or p["db"]
        _ensure_peer(p["host"])
        proc = await asyncio.create_subprocess_exec(
            "pg_dump",
            "-h",
            p["host"],
            "-p",
            str(p["port"]),
            "-U",
            p["user"],
            "-d",
            db,
            "--no-owner",
            "--no-acl",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_pg_cli_env(p["password"]),
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(stderr.decode(errors="replace"))
        return stdout
    path = Path(cfg.get("path", ""))
    return path.read_bytes()


async def dump_database(conn_id: str, database: str | None = None) -> bytes:
    return await dump_from_cfg(get_db_config(conn_id), database)


async def import_to_cfg(
    cfg: dict, content: bytes, database: str | None = None
) -> dict:
    import asyncio

    if _is_mysql(cfg):
        m = _resolve_mysql(cfg)
        _ensure_peer(m["host"])
        db = database or m["db"]
        proc = await asyncio.create_subprocess_exec(
            "mysql",
            *_mysql_cli_ssl_args(),
            "-h",
            m["host"],
            "-P",
            str(m["port"]),
            "-u",
            m["user"],
            f"-p{m['password']}",
            db,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_mysql_cli_env(),
        )
        stdout, stderr = await proc.communicate(input=content)
        if proc.returncode != 0:
            raise RuntimeError(stderr.decode(errors="replace"))
        return {"ok": True, "log": stdout.decode(errors="replace")}
    if _is_postgres(cfg):
        p = _resolve_postgres(cfg)
        db = database or p["db"]
        _ensure_peer(p["host"])
        proc = await asyncio.create_subprocess_exec(
            "psql",
            "-h",
            p["host"],
            "-p",
            str(p["port"]),
            "-U",
            p["user"],
            "-d",
            db,
            "-v",
            "ON_ERROR_STOP=1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_pg_cli_env(p["password"]),
        )
        stdout, stderr = await proc.communicate(input=content)
        if proc.returncode != 0:
            raise RuntimeError(stderr.decode(errors="replace"))
        return {"ok": True, "log": stdout.decode(errors="replace")}
    sql = content.decode("utf-8", errors="replace")
    conn = _sqlite_conn(cfg)
    try:
        conn.executescript(sql)
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


async def import_sql(
    conn_id: str, content: bytes, database: str | None = None
) -> dict:
    return await import_to_cfg(get_db_config(conn_id), content, database)
