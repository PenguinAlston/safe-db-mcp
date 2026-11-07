from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import socket
import stat
import struct
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.engine import make_url

from safe_db_mcp.config import DataSource


_MAX_MESSAGE_BYTES = 16 * 1024
_MAX_URL_BYTES = 8 * 1024
_DEFAULT_SOCKET = "./run/safedb/credentials.sock"
_DEFAULT_TTL_MINUTES = 420
logger = logging.getLogger("safedb.credentials")


class CredentialUnavailable(RuntimeError):
    """Raised when a datasource has no currently valid local credential."""


@dataclass
class _Credential:
    value: str | None
    expires_at: datetime
    entered_at: datetime
    version: int
    state: str


class InMemoryCredentialManager:
    """Worker-only, process-memory credential store.

    Secret values are deliberately not persisted. Restarting the Worker clears
    credentials and requires the operator to enter a newly issued DSN.
    """

    def __init__(
        self,
        datasources: dict[str, DataSource],
        expiring_window: timedelta = timedelta(minutes=30),
        validator: Callable[[DataSource, str], bool] | None = None,
    ):
        self._datasources = datasources
        self._expiring_window = expiring_window
        self._validator = validator
        self._records: dict[str, _Credential] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Credential expiry must include a timezone")
        return value.astimezone(UTC)

    @staticmethod
    def _validate_url(datasource: DataSource, value: str) -> None:
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > _MAX_URL_BYTES:
            raise ValueError("Database URL is empty or exceeds the size limit")
        if any(ord(char) < 32 for char in value):
            raise ValueError("Database URL contains control characters")
        try:
            parsed = make_url(value)
            backend = parsed.get_backend_name()
        except Exception:
            raise ValueError("Database URL is invalid") from None
        expected = {
            "postgres": {"postgresql"},
            "mysql": {"mysql"},
            "oracle": {"oracle"},
        }.get(datasource.dialect, set())
        if backend not in expected:
            raise ValueError("Database URL dialect does not match the datasource")

    def _refresh_locked(self, datasource_id: str, now: datetime) -> bool:
        record = self._records.get(datasource_id)
        if record is None:
            return False
        old_state = record.state
        if record.expires_at <= now:
            record.value = None
            record.state = "NEEDS_CREDENTIAL"
        elif record.expires_at - now <= self._expiring_window:
            record.state = "EXPIRING"
        else:
            record.state = "ACTIVE"
        return old_state != record.state

    def set_credential(
        self,
        datasource_id: str,
        database_url: str,
        expires_at: datetime,
    ) -> dict[str, Any]:
        datasource = self._datasources.get(datasource_id)
        if datasource is None:
            raise ValueError("Datasource is not registered")
        if datasource.credential_mode != "local_manual":
            raise ValueError("Datasource is not configured for local manual credentials")
        self._validate_url(datasource, database_url)
        expiry = self._utc(expires_at)
        entered = datetime.now(UTC)
        remaining = (expiry - entered).total_seconds()
        max_seconds = datasource.max_credential_ttl_minutes * 60
        if remaining <= 0:
            raise ValueError("Credential expiry must be in the future")
        if remaining > max_seconds + 60:
            raise ValueError("Credential expiry exceeds the datasource TTL limit")

        # Validate before replacing the active credential. A broken or unreachable
        # new DSN must never evict the previous still-valid credential.
        if self._validator is not None:
            try:
                if self._validator(datasource, database_url) is not True:
                    raise ValueError("Credential validation failed")
            except Exception:
                raise ValueError("Credential validation failed") from None

        with self._lock:
            previous = self._records.get(datasource_id)
            version = (previous.version + 1) if previous else 1
            self._records[datasource_id] = _Credential(
                value=database_url,
                expires_at=expiry,
                entered_at=entered,
                version=version,
                state="ACTIVE",
            )
            self._refresh_locked(datasource_id, entered)
            return self._status_locked(datasource_id, entered)

    def get_url(self, datasource_id: str) -> str:
        with self._lock:
            now = datetime.now(UTC)
            self._refresh_locked(datasource_id, now)
            record = self._records.get(datasource_id)
            if record is None or not record.value or record.state == "NEEDS_CREDENTIAL":
                raise CredentialUnavailable("Datasource credential is missing or expired")
            return record.value

    def is_available(self, datasource_id: str) -> bool:
        datasource = self._datasources.get(datasource_id)
        if datasource is None:
            return False
        if datasource.credential_mode != "local_manual":
            return True
        with self._lock:
            now = datetime.now(UTC)
            self._refresh_locked(datasource_id, now)
            record = self._records.get(datasource_id)
            return bool(
                record and record.value
                and record.state in {"ACTIVE", "EXPIRING"}
            )

    def clear_credential(self, datasource_id: str) -> dict[str, Any]:
        datasource = self._datasources.get(datasource_id)
        if datasource is None:
            raise ValueError("Datasource is not registered")
        if datasource.credential_mode != "local_manual":
            raise ValueError("Datasource is not configured for local manual credentials")
        with self._lock:
            previous = self._records.get(datasource_id)
            version = (previous.version + 1) if previous else 1
            now = datetime.now(UTC)
            self._records[datasource_id] = _Credential(
                value=None,
                expires_at=now,
                entered_at=now,
                version=version,
                state="NEEDS_CREDENTIAL",
            )
            return self._status_locked(datasource_id, now)

    def _status_locked(self, datasource_id: str, now: datetime) -> dict[str, Any]:
        datasource = self._datasources[datasource_id]
        if datasource.credential_mode != "local_manual":
            return {
                "datasource_id": datasource_id,
                "credential_mode": datasource.credential_mode,
                "state": "EXTERNAL",
                "version": None,
                "entered_at": None,
                "expires_at": None,
            }
        self._refresh_locked(datasource_id, now)
        record = self._records.get(datasource_id)
        if record is None:
            return {
                "datasource_id": datasource_id,
                "credential_mode": "local_manual",
                "state": "MISSING",
                "version": 0,
                "entered_at": None,
                "expires_at": None,
            }
        return {
            "datasource_id": datasource_id,
            "credential_mode": "local_manual",
            "state": record.state,
            "version": record.version,
            "entered_at": record.entered_at.isoformat(),
            "expires_at": record.expires_at.isoformat(),
        }

    def status(self, datasource_id: str) -> dict[str, Any]:
        if datasource_id not in self._datasources:
            raise ValueError("Datasource is not registered")
        with self._lock:
            return self._status_locked(datasource_id, datetime.now(UTC))

    def list_status(self) -> list[dict[str, Any]]:
        with self._lock:
            now = datetime.now(UTC)
            return [
                self._status_locked(datasource_id, now)
                for datasource_id in sorted(self._datasources)
            ]

    def refresh_statuses(self) -> list[dict[str, Any]]:
        """Return status transitions so the Worker can emit secret-free alerts."""
        changed: list[dict[str, Any]] = []
        with self._lock:
            now = datetime.now(UTC)
            for datasource_id, datasource in self._datasources.items():
                if datasource.credential_mode != "local_manual":
                    continue
                if self._refresh_locked(datasource_id, now):
                    changed.append(self._status_locked(datasource_id, now))
        return changed


def validate_database_credential(datasource: DataSource, database_url: str) -> bool:
    """Verify a candidate DSN before it can replace the active local credential."""

    class CandidateProvider:
        def get_url(self, datasource_id: str) -> str:
            if datasource_id != datasource.datasource_id:
                raise CredentialUnavailable("Datasource mismatch")
            return database_url

    from safe_db_mcp.adapters.factory import create_adapter

    adapter = create_adapter(datasource, CandidateProvider())
    try:
        if not adapter.verify_readonly():
            return False
        return adapter.verify_allowed_tables(tuple(sorted(datasource.allowed_tables)))
    finally:
        adapter.close()


class CredentialSocketServer:
    """Local Unix-domain management socket; never exposed over the network."""

    def __init__(self, manager: InMemoryCredentialManager, socket_path: str):
        if not hasattr(socket, "AF_UNIX"):
            raise RuntimeError("Local credential management requires Unix domain sockets")
        self.manager = manager
        self.socket_path = Path(socket_path)
        self._socket: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        parent = self.socket_path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent_stat = parent.lstat()
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or parent_stat.st_uid != os.geteuid()
            or stat.S_IMODE(parent_stat.st_mode) & 0o077
        ):
            raise RuntimeError(
                "Credential socket directory must be Worker-owned and private (0700)"
            )

        try:
            existing = self.socket_path.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.geteuid():
                raise RuntimeError("Refusing to replace an unexpected credential socket path")
            self.socket_path.unlink()

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            server.listen(8)
            server.settimeout(0.5)
        except Exception:
            server.close()
            try:
                self.socket_path.unlink()
            except FileNotFoundError:
                pass
            raise
        self._socket = server
        self._thread = threading.Thread(
            target=self._serve, name="safedb-credential-socket", daemon=True
        )
        self._thread.start()

    def _serve(self) -> None:
        assert self._socket is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._socket.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                continue
            with conn:
                conn.settimeout(5)
                self._handle(conn)

    def _read_message(self, conn: socket.socket) -> dict[str, Any]:
        data = bytearray()
        while len(data) <= _MAX_MESSAGE_BYTES:
            chunk = conn.recv(min(4096, _MAX_MESSAGE_BYTES + 1 - len(data)))
            if not chunk:
                break
            newline = chunk.find(b"\n")
            if newline >= 0:
                data.extend(chunk[:newline])
                break
            data.extend(chunk)
        if not data or len(data) > _MAX_MESSAGE_BYTES:
            raise ValueError("Invalid request")
        parsed = json.loads(data.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("Invalid request")
        return parsed

    def _handle(self, conn: socket.socket) -> None:
        peer_uid: int | str = "unknown"
        try:
            if hasattr(socket, "SO_PEERCRED"):
                raw = conn.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
                )
                _, peer_uid, _ = struct.unpack("3i", raw)
                if peer_uid != os.geteuid():
                    raise PermissionError("Credential socket peer is not authorized")
            request = self._read_message(conn)
            operation = request.get("operation")
            if operation == "set":
                status = self.manager.set_credential(
                    request.get("datasource_id"),
                    request.get("database_url"),
                    datetime.fromisoformat(request.get("expires_at")),
                )
                logger.info(
                    "datasource_credential.installed datasource=%s actor_uid=%s "
                    "version=%s expires_at=%s",
                    status["datasource_id"], peer_uid, status["version"],
                    status["expires_at"],
                )
                response = {"ok": True, "status": status}
            elif operation == "status":
                datasource_id = request.get("datasource_id")
                response = {
                    "ok": True,
                    "statuses": (
                        [self.manager.status(datasource_id)]
                        if datasource_id else self.manager.list_status()
                    ),
                }
            elif operation == "clear":
                status = self.manager.clear_credential(request.get("datasource_id"))
                logger.warning(
                    "datasource_credential.cleared datasource=%s actor_uid=%s version=%s",
                    status["datasource_id"], peer_uid, status["version"],
                )
                response = {"ok": True, "status": status}
            else:
                raise ValueError("Unsupported operation")
        except Exception as exc:
            # Never log/return exception text: it may contain supplied secrets.
            logger.warning(
                "datasource_credential.request_rejected actor_uid=%s error_type=%s",
                peer_uid, type(exc).__name__,
            )
            response = {"ok": False, "error": "Request rejected"}
        try:
            conn.sendall(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")
        except OSError:
            pass

    def close(self) -> None:
        self._stop.set()
        if self._socket is not None:
            self._socket.close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        try:
            if self.socket_path.is_socket():
                self.socket_path.unlink()
        except FileNotFoundError:
            pass


class CredentialSocketClient:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(data) > _MAX_MESSAGE_BYTES:
            raise RuntimeError("Credential request exceeds the size limit")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            # Credential replacement performs bounded DB read-only/catalog checks.
            client.settimeout(30)
            client.connect(self.socket_path)
            client.sendall(data)
            response = bytearray()
            while len(response) <= _MAX_MESSAGE_BYTES:
                chunk = client.recv(min(4096, _MAX_MESSAGE_BYTES + 1 - len(response)))
                if not chunk:
                    break
                newline = chunk.find(b"\n")
                if newline >= 0:
                    response.extend(chunk[:newline])
                    break
                response.extend(chunk)
        parsed = json.loads(response.decode("utf-8"))
        if not isinstance(parsed, dict) or not parsed.get("ok"):
            raise RuntimeError("Credential operation failed; verify datasource and Worker state")
        return parsed


def _parse_expiry(args: argparse.Namespace) -> datetime:
    if args.expires_at:
        try:
            value = datetime.fromisoformat(args.expires_at.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("--expires-at must be ISO-8601 with a timezone") from None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("--expires-at must include a timezone")
        return value.astimezone(UTC)
    ttl = args.ttl_minutes if args.ttl_minutes is not None else _DEFAULT_TTL_MINUTES
    if not 1 <= ttl <= 480:
        raise ValueError("--ttl-minutes must be between 1 and 480")
    return datetime.now(UTC) + timedelta(minutes=ttl)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Manage Worker-local temporary database credentials."
    )
    parser.add_argument(
        "--socket",
        default=os.getenv("SAFEDB_CREDENTIAL_SOCKET", _DEFAULT_SOCKET),
        help="Worker Unix socket path",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    set_cmd = commands.add_parser("set", help="Install a manually issued temporary DSN")
    set_cmd.add_argument("datasource_id")
    set_cmd.add_argument("--ttl-minutes", type=int)
    set_cmd.add_argument("--expires-at", help="ISO-8601 timestamp with timezone")
    status_cmd = commands.add_parser("status", help="Show credential state without secrets")
    status_cmd.add_argument("datasource_id", nargs="?")
    clear_cmd = commands.add_parser("clear", help="Remove a local credential")
    clear_cmd.add_argument("datasource_id")
    args = parser.parse_args(argv)

    client = CredentialSocketClient(args.socket)
    try:
        if args.command == "set":
            if args.expires_at and args.ttl_minutes is not None:
                raise ValueError("Use either --expires-at or --ttl-minutes")
            expiry = _parse_expiry(args)
            database_url = getpass.getpass("Temporary database URL (input hidden): ")
            confirmation = getpass.getpass("Confirm temporary database URL: ")
            if not database_url or database_url != confirmation:
                raise ValueError("Database URLs are empty or do not match")
            response = client.request({
                "operation": "set",
                "datasource_id": args.datasource_id,
                "database_url": database_url,
                "expires_at": expiry.isoformat(),
            })
            status = response["status"]
            print(
                f"Credential installed for {status['datasource_id']}; "
                f"version={status['version']}; expires_at={status['expires_at']}"
            )
        elif args.command == "status":
            response = client.request({
                "operation": "status",
                "datasource_id": args.datasource_id,
            })
            for status in response["statuses"]:
                print(
                    f"{status['datasource_id']}: {status['state']} "
                    f"(version={status['version']}, expires_at={status['expires_at']})"
                )
        else:
            status = client.request({
                "operation": "clear",
                "datasource_id": args.datasource_id,
            })["status"]
            print(f"Credential cleared for {status['datasource_id']}")
    except Exception as exc:
        # Do not include raw exception details; they may contain supplied secrets.
        print(f"Credential operation failed: {type(exc).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
