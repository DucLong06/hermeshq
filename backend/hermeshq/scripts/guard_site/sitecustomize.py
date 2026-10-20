import importlib.util
import os

try:
    _spec = importlib.util.spec_from_file_location(
        "hermeshq_workspace_guard", os.path.join(os.path.dirname(os.path.abspath(__file__)), "workspace_guard.py")
    )
    _guard = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_guard)
    for _candidate in (os.environ.get("HERMES_SESSION_CWD"), os.getcwd(), os.environ.get("HOME")):
        if _candidate and _guard.install(_candidate):
            break
except Exception:  # noqa: BLE001  # the guard must never stop an interpreter from starting
    pass
