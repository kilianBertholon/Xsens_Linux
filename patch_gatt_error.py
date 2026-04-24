import re

with open("xdot_manager/sensor.py", "r") as f:
    content = f.read()

# Hook the disconnect error handler
hook_code = """
    def _check_disconnect_error(self, exc: Exception) -> None:
        "Détecte une déconnexion silencieuse sur erreur GATT."
        err_msg = str(exc).lower()
        if "not connected" in err_msg or "unreachable" in err_msg or "unlikely error" in err_msg:
            if self.state != DotState.DISCONNECTED:
                logger.warning("[%s] Erreur GATT fatale détectée (%s) -> Force déconnexion.", self.name, exc)
                self._bleak_disconnected_cb(self._client)
"""

if "_check_disconnect_error" not in content:
    content = content.replace("    def _bleak_disconnected_cb", hook_code + "\n    def _bleak_disconnected_cb")

from textwrap import dedent

# For write_command
content = re.sub(
    r'(async with sem:\s+await asyncio\.wait_for\([\s\S]*?\n\s+\)[\s\S]*?except asyncio\.TimeoutError:\s+raise DotTimeoutError.*?$)',
    r'\1\n        except (BleakError, OSError) as exc:\n            self._check_disconnect_error(exc)\n            raise',
    content, flags=re.MULTILINE
)

# For read_ack
content = re.sub(
    r'(async with sem:\s+raw = await asyncio\.wait_for\([\s\S]*?timeout=GATT_TIMEOUT,\s+\)\s+except asyncio\.TimeoutError:\s+raise DotTimeoutError.*?$)',
    r'\1\n        except (BleakError, OSError) as exc:\n            self._check_disconnect_error(exc)\n            raise',
    content, flags=re.MULTILINE
)

# Wait, this regex might be tricky. Let's just use Python's ast / manually edit.
with open("xdot_manager/sensor.py", "w") as f:
    f.write(content)
