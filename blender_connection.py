"""
Blender Connection Module
Provides a TCP socket client to communicate with the BlenderMCP server
running inside Blender. Uses the same JSON-over-TCP protocol as blender-mcp.
"""

import json
import logging
import os
import socket
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("BlenderConnection")

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 9876


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
            logger.info(f"Connected to Blender at {self.host}:{self.port}")
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
                        logger.info(
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
            logger.info(
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
        """Send a command to Blender and return the response."""
        if not self.sock and not self.connect():
            raise ConnectionError("Not connected to Blender")

        command = {"type": command_type, "params": params or {}}

        try:
            logger.info(
                f"Sending command: {command_type} with params: {params}"
            )
            self.sock.sendall(json.dumps(command).encode("utf-8"))
            logger.info("Command sent, waiting for response...")

            self.sock.settimeout(180.0)
            response_data = self._receive_full_response(self.sock)
            logger.info(f"Received {len(response_data)} bytes of data")

            response = json.loads(response_data.decode("utf-8"))
            logger.info(
                f"Response parsed, status: {response.get('status', 'unknown')}"
            )

            if response.get("status") == "error":
                logger.error(f"Blender error: {response.get('message')}")
                raise Exception(
                    response.get("message", "Unknown error from Blender")
                )

            return response.get("result", {})

        except socket.timeout:
            logger.error(
                "Socket timeout while waiting for response from Blender"
            )
            self.sock = None
            raise Exception(
                "Timeout waiting for Blender response - try simplifying your request"
            )
        except (
            ConnectionError,
            BrokenPipeError,
            ConnectionResetError,
        ) as e:
            logger.error(f"Socket connection error: {str(e)}")
            self.sock = None
            raise Exception(f"Connection to Blender lost: {str(e)}")
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON response from Blender: {str(e)}")
            self.sock = None
            raise Exception(f"Invalid response from Blender: {str(e)}")
        except Exception as e:
            logger.error(f"Error communicating with Blender: {str(e)}")
            self.sock = None
            raise Exception(f"Communication error with Blender: {str(e)}")


# ──────────────────────────────────────────────
# Singleton connection management
# ──────────────────────────────────────────────

_blender_connection: Optional[BlenderConnection] = None


def get_blender_connection() -> BlenderConnection:
    """Get or create a persistent Blender connection (singleton)."""
    global _blender_connection

    # Validate existing connection with a lightweight socket check (no TCP round-trip)
    if _blender_connection is not None:
        try:
            if _blender_connection.sock is not None:
                _blender_connection.sock.getpeername()
                return _blender_connection
            else:
                raise Exception("Socket is None")
        except Exception as e:
            logger.warning(
                f"Existing connection is no longer valid: {str(e)}"
            )
            try:
                _blender_connection.disconnect()
            except:
                pass
            _blender_connection = None

    # Create new connection
    if _blender_connection is None:
        host = os.getenv("BLENDER_HOST", DEFAULT_HOST)
        port = int(os.getenv("BLENDER_PORT", str(DEFAULT_PORT)))
        _blender_connection = BlenderConnection(host=host, port=port)
        if not _blender_connection.connect():
            logger.error("Failed to connect to Blender")
            _blender_connection = None
            raise Exception(
                "Could not connect to Blender. Make sure Blender is running with the addon."
            )
        logger.info("Created new persistent connection to Blender")

    return _blender_connection


def close_blender_connection():
    """Close the global Blender connection."""
    global _blender_connection
    if _blender_connection:
        _blender_connection.disconnect()
        _blender_connection = None
        logger.info("Blender connection closed")
