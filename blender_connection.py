"""
Blender Connection Module
Provides a TCP socket client to communicate with the BlenderMCP server
running inside Blender. Uses the same JSON-over-TCP protocol as blender-mcp.
"""

import json
import logging
import os
import select
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("BlenderConnection")

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 9876

# Retry configuration (overridable via environment variables)
CONNECT_MAX_RETRIES = int(os.getenv("BLENDER_CONNECT_RETRIES", "5"))
COMMAND_MAX_RETRIES = int(os.getenv("BLENDER_COMMAND_RETRIES", "3"))
RETRY_BACKOFF_BASE = float(os.getenv("BLENDER_RETRY_BACKOFF", "1.0"))

# Transient errors that warrant a retry
_TRANSIENT_ERRORS = (ConnectionError, BrokenPipeError, ConnectionResetError, socket.timeout, OSError)


@dataclass
class BlenderConnection:
    """TCP socket client for communicating with Blender's addon server."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    sock: Optional[socket.socket] = field(default=None, repr=False)

    def connect(self) -> bool:
        """Connect to the Blender addon socket server."""
        if self.sock:
            return True

        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((self.host, self.port))
            logger.debug(f"Connected to Blender at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Blender: {str(e)}")
            self.sock = None
            return False

    def disconnect(self):
        """Disconnect from the Blender addon."""
        if self.sock:
            try:
                self.sock.close()
            except Exception as e:
                logger.error(f"Error disconnecting from Blender: {str(e)}")
            finally:
                self.sock = None

    def _receive_full_response(
        self, sock: socket.socket, buffer_size: int = 8192
    ) -> bytes:
        """Receive the complete JSON response, potentially in multiple chunks."""
        chunks = []
        sock.settimeout(180.0)

        try:
            while True:
                try:
                    chunk = sock.recv(buffer_size)
                    if not chunk:
                        if not chunks:
                            raise Exception(
                                "Connection closed before receiving any data"
                            )
                        break

                    chunks.append(chunk)

                    # Try to parse what we have so far
                    try:
                        data = b"".join(chunks)
                        json.loads(data.decode("utf-8"))
                        logger.debug(
                            f"Received complete response ({len(data)} bytes)"
                        )
                        return data
                    except json.JSONDecodeError:
                        continue

                except socket.timeout:
                    logger.warning("Socket timeout during chunked receive")
                    break
                except (
                    ConnectionError,
                    BrokenPipeError,
                    ConnectionResetError,
                ) as e:
                    logger.error(
                        f"Socket connection error during receive: {str(e)}"
                    )
                    raise
        except socket.timeout:
            logger.warning("Socket timeout during chunked receive")
        except Exception as e:
            logger.error(f"Error during receive: {str(e)}")
            raise

        if chunks:
            data = b"".join(chunks)
            logger.debug(
                f"Returning data after receive completion ({len(data)} bytes)"
            )
            try:
                json.loads(data.decode("utf-8"))
                return data
            except json.JSONDecodeError:
                raise Exception("Incomplete JSON response received")
        else:
            raise Exception("No data received")

    def send_command(
        self, command_type: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Send a command to Blender and return the response.

        On transient connection errors, automatically retries up to
        COMMAND_MAX_RETRIES times with exponential backoff.
        Blender application-level errors are never retried.
        """
        last_error: Optional[Exception] = None

        for attempt in range(1, COMMAND_MAX_RETRIES + 1):
            # Ensure we have a live socket
            if not self.sock and not self.connect():
                last_error = ConnectionError("Not connected to Blender")
                if attempt < COMMAND_MAX_RETRIES:
                    delay = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                    logger.warning(
                        "send_command connect failed (attempt %d/%d), retrying in %.1fs",
                        attempt, COMMAND_MAX_RETRIES, delay,
                    )
                    time.sleep(delay)
                    continue
                raise last_error

            command = {"type": command_type, "params": params or {}}

            try:
                logger.debug(f"Sending command: {command_type} (attempt {attempt}/{COMMAND_MAX_RETRIES})")
                self.sock.sendall(json.dumps(command).encode("utf-8"))
                logger.debug("Command sent, waiting for response...")

                self.sock.settimeout(180.0)
                response_data = self._receive_full_response(self.sock)
                logger.debug(f"Received {len(response_data)} bytes of data")

                response = json.loads(response_data.decode("utf-8"))
                logger.debug(
                    f"Response parsed, status: {response.get('status', 'unknown')}"
                )

                # Application-level Blender errors are NOT retried
                if response.get("status") == "error":
                    logger.error(f"Blender error: {response.get('message')}")
                    raise Exception(
                        response.get("message", "Unknown error from Blender")
                    )

                return response.get("result", {})

            except _TRANSIENT_ERRORS as e:
                # Transient connection/socket error — invalidate and retry
                logger.warning(
                    "Transient error on attempt %d/%d for '%s': %s",
                    attempt, COMMAND_MAX_RETRIES, command_type, e,
                )
                self.disconnect()
                last_error = e
                if attempt < COMMAND_MAX_RETRIES:
                    if not _is_blender_alive():
                        logger.error("Blender process is not running — skipping remaining retries")
                        raise Exception(
                            f"Blender process is not running. Connection lost after {attempt} attempt(s)."
                        )
                    delay = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                    logger.warning("Retrying in %.1fs…", delay)
                    time.sleep(delay)
                    continue
                raise Exception(
                    f"Connection to Blender lost after {COMMAND_MAX_RETRIES} attempts: {e}"
                )

            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON response from Blender: {str(e)}")
                self.disconnect()
                raise Exception(f"Invalid response from Blender: {str(e)}")

            except Exception as e:
                # Non-transient error (e.g. Blender app error) — do not retry
                error_msg = str(e)
                if self.sock is None or not error_msg:
                    self.disconnect()
                logger.error(f"Error communicating with Blender: {error_msg}")
                raise

        # Should not reach here, but safety net
        raise Exception(
            f"Failed to send command '{command_type}' after {COMMAND_MAX_RETRIES} attempts"
        )


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────


def _is_blender_alive() -> bool:
    """Check whether a Blender process is running on this machine."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "blender"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0
    except FileNotFoundError:
        # pgrep not available (e.g. minimal container) — assume alive
        return True


def _is_socket_healthy(sock: socket.socket) -> bool:
    """Lightweight check: socket is connected and has no pending errors."""
    try:
        sock.getpeername()
        # select with timeout 0 → check if socket is readable (would mean
        # data waiting or EOF/error). A healthy idle socket is NOT readable.
        readable, _, errored = select.select([sock], [], [sock], 0)
        if errored:
            return False
        if readable:
            # Peek without consuming — if recv returns b'' the peer closed.
            data = sock.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
            if not data:
                return False
        return True
    except Exception:
        return False


# ──────────────────────────────────────────────
# Singleton connection management
# ──────────────────────────────────────────────

_blender_connection: Optional[BlenderConnection] = None
_connection_lock = threading.Lock()


def get_blender_connection() -> BlenderConnection:
    """Get or create a persistent Blender connection (singleton).

    Thread-safe. Retries with exponential backoff up to CONNECT_MAX_RETRIES
    times if the initial connection attempt fails.
    """
    global _blender_connection

    with _connection_lock:
        # Validate existing connection
        if _blender_connection is not None:
            if _blender_connection.sock is not None and _is_socket_healthy(_blender_connection.sock):
                return _blender_connection
            # Socket is dead — tear down
            reason = "socket dropped" if _blender_connection.sock is not None else "socket is None"
            logger.warning("Existing connection is no longer valid: %s", reason)
            try:
                _blender_connection.disconnect()
            except Exception:
                pass
            _blender_connection = None

        # Create new connection with retry + backoff
        host = os.getenv("BLENDER_HOST", DEFAULT_HOST)
        port = int(os.getenv("BLENDER_PORT", str(DEFAULT_PORT)))

        for attempt in range(1, CONNECT_MAX_RETRIES + 1):
            conn = BlenderConnection(host=host, port=port)
            if conn.connect():
                _blender_connection = conn
                logger.info(
                    "Connected to Blender at %s:%s (attempt %d/%d)",
                    host, port, attempt, CONNECT_MAX_RETRIES,
                )
                return _blender_connection

            # Connection failed — check if Blender is even running
            if not _is_blender_alive():
                logger.error("Blender process is not running — aborting connection attempts")
                raise Exception(
                    "Blender process is not running. Cannot establish connection."
                )

            if attempt < CONNECT_MAX_RETRIES:
                delay = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                logger.warning(
                    "Connection attempt %d/%d failed, retrying in %.1fs…",
                    attempt, CONNECT_MAX_RETRIES, delay,
                )
                time.sleep(delay)

        raise Exception(
            f"Could not connect to Blender after {CONNECT_MAX_RETRIES} attempts. "
            "Make sure Blender is running with the addon."
        )


def close_blender_connection():
    """Close the global Blender connection."""
    global _blender_connection
    if _blender_connection:
        _blender_connection.disconnect()
        _blender_connection = None
        logger.debug("Blender connection closed")
