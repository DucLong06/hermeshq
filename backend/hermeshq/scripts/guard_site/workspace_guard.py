import os
import re
import sys

_AGENT_DIR = re.compile(r"^(?P<root>.*?/)(?P<agent>agent-[^/]+)(?:/|$)")
_PATH_ARGUMENTS = {
    "open": 1,
    "glob.glob": 1,
    "os.listdir": 1,
    "os.scandir": 1,
    "os.remove": 1,
    "os.mkdir": 1,
    "os.rmdir": 1,
    "os.chmod": 1,
    "os.truncate": 1,
    "os.chdir": 1,
    "shutil.rmtree": 1,
    "os.rename": 2,
    "os.replace": 2,
    "os.symlink": 2,
    "os.link": 2,
    "shutil.copyfile": 2,
    "shutil.copymode": 2,
    "shutil.copystat": 2,
    "shutil.copytree": 2,
    "shutil.move": 2,
}
_SYSTEM_PREFIXES = ("/usr/", "/lib", "/proc/", "/sys/", "/dev/", "/etc/", "/bin/", "/sbin/", "/opt/")


def own_agent_dir(cwd: str) -> tuple[str, str] | None:
    match = _AGENT_DIR.match(os.path.abspath(cwd))
    if not match:
        return None
    return match.group("root"), match.group("agent")


def other_agent_dir(path: str, root: str, own: str) -> str | None:
    match = _AGENT_DIR.match(path)
    if match and match.group("root") == root and match.group("agent") != own:
        return match.group("agent")
    return None


def _as_text(value) -> str | None:
    if isinstance(value, bytes):
        try:
            return os.fsdecode(value)
        except ValueError:
            return None
    if isinstance(value, os.PathLike):
        try:
            value = os.fspath(value)
        except (TypeError, ValueError):
            return None
        return _as_text(value)
    return value if isinstance(value, str) else None


def blocked(value, root: str, own: str, cwd: str) -> str | None:
    text = _as_text(value)
    if not text:
        return None
    absolute = os.path.normpath(text if text.startswith("/") else os.path.join(cwd, text))
    if absolute.startswith(_SYSTEM_PREFIXES) or not absolute.startswith(root):
        return None
    other = other_agent_dir(absolute, root, own)
    if other:
        return other
    try:
        real = os.path.realpath(absolute)
    except OSError:
        return None
    return other_agent_dir(real, root, own)


def command_mentions_other_agent(arguments, root: str, own: str) -> str | None:
    pattern = re.compile(re.escape(root) + r"(agent-[A-Za-z0-9-]+)")
    for item in arguments if isinstance(arguments, (list, tuple)) else [arguments]:
        text = _as_text(item)
        if not text or "agent-" not in text:
            continue
        for match in pattern.finditer(text):
            if match.group(1) != own:
                return match.group(1)
    return None


def install(cwd: str) -> bool:
    located = own_agent_dir(cwd)
    if located is None or getattr(sys, "_hermeshq_workspace_guard", False):
        return False
    root, own = located
    sys._hermeshq_workspace_guard = True
    busy = {"on": False}

    def hook(event, args):
        if busy["on"]:
            return
        paths = _PATH_ARGUMENTS.get(event)
        if paths is None and event != "subprocess.Popen":
            return
        busy["on"] = True
        try:
            try:
                current = os.getcwd()
            except OSError:
                current = cwd
            if paths is None:
                partes = [args[0], *(args[1] if isinstance(args[1], (list, tuple)) else [args[1]])]
                if len(args) > 2:
                    partes.append(args[2])
                other = command_mentions_other_agent(partes, root, own)
            else:
                other = next((o for o in (blocked(a, root, own, current) for a in args[:paths]) if o), None)
            if other:
                raise PermissionError(
                    f"Access denied: the folder of another agent ({other}) is not available to this agent."
                )
        finally:
            busy["on"] = False

    sys.addaudithook(hook)
    return True
