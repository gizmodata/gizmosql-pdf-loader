"""Connection settings for the target GizmoSQL server."""

from __future__ import annotations

import os
from dataclasses import dataclass

from adbc_driver_gizmosql import DatabaseOptions
from adbc_driver_gizmosql import dbapi as gizmosql

DEFAULT_MAX_MESSAGE_SIZE_BYTES = 64 * 1024 * 1024  # client-side gRPC limit; driver default is 16 MiB
EPHEMERAL_CATALOGS = frozenset({"memory", "temp"})


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass
class GizmoSQLSettings:
    hostname: str
    port: int = 31337
    username: str | None = None
    password: str | None = None
    use_tls: bool = True
    tls_skip_verify: bool = False
    max_message_size_bytes: int = DEFAULT_MAX_MESSAGE_SIZE_BYTES

    @classmethod
    def from_env(cls) -> GizmoSQLSettings:
        hostname = os.environ.get("GIZMOSQL_HOSTNAME")
        if not hostname:
            raise ValueError("GIZMOSQL_HOSTNAME is not set (put it in .env or the environment)")
        return cls(
            hostname=hostname,
            port=int(os.environ.get("GIZMOSQL_PORT", "31337")),
            username=os.environ.get("GIZMOSQL_USERNAME"),
            password=os.environ.get("GIZMOSQL_PASSWORD"),
            use_tls=_env_bool(name="GIZMOSQL_USE_TLS", default=True),
            tls_skip_verify=_env_bool(name="GIZMOSQL_TLS_SKIP_VERIFY", default=False),
            max_message_size_bytes=int(
                os.environ.get("GIZMOSQL_MAX_MESSAGE_SIZE_BYTES", str(DEFAULT_MAX_MESSAGE_SIZE_BYTES))
            ),
        )

    @property
    def uri(self) -> str:
        uri = f"gizmosql://{self.hostname}:{self.port}"
        if not self.use_tls:
            uri += "?transport=tcp"
        return uri

    def connect(self, *, catalog: str | None = None, db_schema: str | None = None) -> gizmosql.Connection:
        """Open a DBAPI connection with the gRPC max-message-size raised.

        ``catalog`` becomes the session's current catalog. It must already be attached on the
        server (attaching requires the GizmoSQL admin role).
        """
        return gizmosql.connect(
            self.uri,
            username=self.username,
            password=self.password,
            tls_skip_verify=self.tls_skip_verify,
            catalog=catalog,
            db_schema=db_schema,
            db_kwargs={DatabaseOptions.WITH_MAX_MSG_SIZE.value: str(self.max_message_size_bytes)},
        )


def resolve_target_catalog(conn: gizmosql.Connection, catalog: str | None) -> str:
    """Return the catalog to load into, refusing the server's ephemeral in-memory catalogs.

    A session's default catalog is whatever database the server was started with; on some
    deployments that is the in-memory ``memory`` catalog, and loading there without noticing
    would put every table somewhere that vanishes on restart.
    """
    if catalog:
        return catalog
    with conn.cursor() as cur:
        cur.execute("SELECT current_catalog()")
        current = cur.fetchone()[0]
    if current in EPHEMERAL_CATALOGS:
        raise ValueError(
            f"The session's default catalog on this server is the ephemeral '{current}' catalog. "
            "Pass --catalog (or set GIZMOSQL_CATALOG) to name a persistent catalog."
        )
    return current
