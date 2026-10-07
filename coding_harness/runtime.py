import ast
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

if __package__:
    from . import tool_repository
    from .sandbox import DockerSandbox, SandboxError, snapshot_repository
else:
    import tool_repository
    from sandbox import DockerSandbox, SandboxError, snapshot_repository

LIMIT = 12_000
MAX_LIST = 200
MAX_MATCHES = 30
BASH_TIMEOUT = 20
FETCH_TIMEOUT = 10
MUTATING = {"write_file", "edit_file", "delete_file", "bash"}

class Denied(Exception):
    """Policy refused the request."""

class ToolError(Exception):
    """The request was allowed but could not be completed."""

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ToolError("redirects are not allowed")

def _open_url(url):
    """Open an HTTPS URL without following redirects (patched in tests)."""
    opener = urllib.request.build_opener(_NoRedirect)
    return opener.open(urllib.request.Request(url, method="GET"), timeout=FETCH_TIMEOUT)


def _check_python(path, source):
    """Validate Python before committing a file change."""
    if Path(path).suffix != ".py":
        return
    try:
        ast.parse(source, filename=path)
    except (SyntaxError, ValueError) as error:
        raise ToolError(f"invalid Python in {path}: {error}") from None


def _reindented_edit(source, before, after):
    """Recover a unique whole-line edit whose indentation was lost."""
    lines = source.splitlines(keepends=True)
    target = before.splitlines()
    if not target or not any(line.strip() for line in target):
        return None
    locations = [
        start for start in range(len(lines) - len(target) + 1)
        if [line.lstrip().rstrip("\r\n") for line in lines[start:start + len(target)]]
        == [line.lstrip() for line in target]
    ]
    if len(locations) != 1:
        return None
    start = locations[0]
    original = lines[start:start + len(target)]
    anchor = next(index for index, line in enumerate(target) if line.strip())
    actual_indent = original[anchor][:len(original[anchor]) - len(original[anchor].lstrip())]
    given_indent = target[anchor][:len(target[anchor]) - len(target[anchor].lstrip())]
    replacement = []
    for line in after.splitlines(keepends=True):
        if line.strip():
            if given_indent and not line.startswith(given_indent):
                return None
            line = actual_indent + line[len(given_indent):]
        replacement.append(line)
    # A block replacement should not join its last line to the next statement.
    if replacement and original[-1].endswith("\n") and not replacement[-1].endswith("\n"):
        replacement[-1] += "\r\n" if original[-1].endswith("\r\n") else "\n"
    return "".join(lines[:start] + replacement + lines[start + len(target):])


class Runtime:
    """Validate and execute tools."""

    def __init__(self, root, *, approve=None, enabled=None,
                 image="coding-harness-sandbox:local"):
        """Create a runtime.

        Args:
            root: Secret-free target Git repository; only tracked source is copied.
        """
        self._temporary = tempfile.TemporaryDirectory(prefix="coding-harness-")
        self.root = Path(self._temporary.name) / "workspace"
        self.root.mkdir()
        try:
            snapshot_repository(root, self.root)
        except BaseException:
            self._temporary.cleanup()
            raise
        # The container runs as an unprivileged user on Linux.
        self.root.chmod(0o777)
        for entry in self.root.rglob("*"):
            entry.chmod(0o777 if entry.is_dir() or entry.stat().st_mode & 0o111 else 0o666)
        self.sandbox = DockerSandbox(self.root, image=image)
        self.approve = approve
        self.bash_timeout = BASH_TIMEOUT
        self.tools = [tool for tool in tool_repository.describe_tools()
                      if enabled is None or tool["name"] in enabled]

    def close(self):
        if self.sandbox.broken:
            self._temporary._finalizer.detach()
            raise SandboxError("cleanup not confirmed; retaining disposable workspace")
        self._temporary.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    #POLICIES
    def tool_allowed(self, name):
        """True when the tool is enabled."""
        return any(tool["name"] == name for tool in self.tools)

    def execute(self, action):
        """Execute the given action."""
        try:
            if self.sandbox.broken:
                raise Denied("sandbox cleanup failed; further actions disabled")
            if not isinstance(action, dict):
                raise ToolError('action must be {"tool": str, "args": object}')

            if set(action) != {"tool", "args"}:
                raise ToolError('action must be {"tool": str, "args": object}')

            name = action["tool"]
            args = action["args"]

            if not isinstance(name, str):
                raise ToolError("tool name must be a string")

            if not self.tool_allowed(name):
                raise ToolError(f"unknown tool: {name!r}")

            expected_args = tool_repository.get_tool(name)["args"]
            argument_error = f"{name} takes exactly these arguments: {expected_args}"

            if not isinstance(args, dict):
                raise ToolError(argument_error)

            if set(args) != set(expected_args):
                raise ToolError(argument_error)

            for key, value in args.items():
                error_message = (
                    f"argument {key!r} must be a string "
                    f"of at most {LIMIT} chars"
                )

                if not isinstance(value, str) or len(value) > LIMIT:
                    raise ToolError(error_message)

            handler = getattr(self, "_" + name)
            result = handler(**args)
            return {"status": "ok", "output": result}

        except Denied as error:
            return {
                "status": "denied",
                "output": str(error),
            }

        except (ToolError, SandboxError, subprocess.SubprocessError) as error:
            return {
                "status": "error",
                "output": error.args[0] if error.args else str(error),
            }

        except (OSError, UnicodeError) as error:
            return {
                "status": "error",
                "output": f"{error.__class__.__name__}: {error}",
            }

    def _resolve_path(self, path):
        """Map a relative path to a real path inside root, or raise Denied."""
        pure_path = PurePosixPath(path)

        if (not path or "\\" in path or ":" in path or "\0" in path
                or pure_path.is_absolute() or Path(path).is_absolute()):
            raise Denied("path must be relative to root using forward slashes")
        if any(part == ".." or part.startswith(".") for part in pure_path.parts):
            raise Denied("'..' and hidden directories are not allowed")
        real_path = (self.root / pure_path).resolve()
        for entry in [(self.root / pure_path), *(self.root / pure_path).parents]:
            if entry == self.root:
                break
            if entry.is_symlink() or entry.is_junction():
                raise Denied("links are not allowed")
        try:
            real_path.relative_to(self.root).as_posix()
        except ValueError:
            raise Denied("path is outside the workspace") from None

        return real_path

    def _walk_workspace(self):
        """Return relative paths of visible files, without following links."""
        for path, dir_names, file_names in os.walk(self.root, followlinks=False):
            visible_dirs = []
            for dirname in dir_names:
                directory = Path(path) / dirname
                if (not dirname.startswith(".") and not directory.is_symlink()
                        and not directory.is_junction()):
                    visible_dirs.append(dirname)
            dir_names[:] = sorted(visible_dirs)

            for filename in sorted(file_names):
                if filename.startswith(".") or (Path(path) / filename).is_symlink():
                    continue
                file_path = Path(path) / filename
                yield file_path.relative_to(self.root).as_posix()

    #TOOL HANDLERS
    def _list_files(self):
        paths = []
        for index, path in enumerate(self._walk_workspace()):
            if index < MAX_LIST:
                paths.append(path)
                continue
            paths.append(f"[truncated at {MAX_LIST} files]")
            break
        return paths

    def _read_file(self, path):
        location = self._resolve_path(path)
        if not location.is_file():
            raise ToolError(f"not a file: {path}")
        try:
            with location.open("r", encoding="utf-8") as stream:
                contents = stream.read(LIMIT + 1)
        except UnicodeDecodeError:
            raise ToolError(f"not UTF-8 text: {path}") from None
        if len(contents) <= LIMIT:
            return contents
        return f"{contents[:LIMIT]}\n[truncated: showing first {LIMIT} characters]"

    def _search_files(self, query):
        if query == "":
            raise ToolError("query must not be empty")
        found = []
        for index, path in enumerate(self._walk_workspace()):
            if index >= MAX_LIST or len(found) >= MAX_MATCHES:
                break
            try:
                location = self._resolve_path(path)
                if not location.is_file():
                    continue
                with location.open("rb") as stream:
                    contents = stream.read(1_000_000)
                if b"\x00" in contents:
                    continue
                lines = contents.decode("utf-8").splitlines()
            except (Denied, OSError, UnicodeDecodeError):
                continue
            for index, line in enumerate(lines, start=1):
                if query not in line:
                    continue
                found.append(dict(path=path, line=index, excerpt=line.strip()[:200]))
                if len(found) >= MAX_MATCHES:
                    break
        return found

    def _write_file(self, path, content):
        destination = self._resolve_path(path)
        self._validate_change(path, content, f"content exceeds {LIMIT} bytes")
        destination.parent.mkdir(parents=True, exist_ok=True)
        for directory in destination.parents:
            directory.chmod(0o777)
            if directory == self.root:
                break
        try:
            with destination.open(mode="x", encoding="utf-8") as stream:
                stream.write(content)
        except FileExistsError:
            raise ToolError(f"{path} already exists; use edit_file") from None
        destination.chmod(0o666)
        return f"created {path}"

    def _edit_file(self, path, old, new):
        destination = self._resolve_path(path)
        if not destination.is_file():
            raise ToolError(f"not a file: {path}")
        if destination.stat().st_size > LIMIT:
            raise ToolError(f"{path} is larger than {LIMIT} bytes")
        if old == "":
            raise ToolError("old text must not be empty")
        contents = destination.read_text(encoding="utf-8")
        occurrences = contents.count(old)
        options = []
        if occurrences == 1:
            options.append((contents.replace(old, new, 1), ""))
        if occurrences <= 1:
            recovered = _reindented_edit(contents, old, new)
            if recovered is not None:
                options.append((recovered, " (old matched once ignoring indentation; new was re-indented)"))
        if not options:
            raise ToolError(f"old text occurs {occurrences} times; it must occur exactly once")
        first_error = None
        for contents, suffix in options:
            try:
                self._validate_change(path, contents, f"edited file would exceed {LIMIT} bytes")
            except ToolError as error:
                if first_error is None:
                    first_error = error
            else:
                destination.write_text(contents, encoding="utf-8")
                return f"edited {path}{suffix}"
        raise first_error

    def _delete_file(self, path):
        destination = self._resolve_path(path)
        if not destination.is_file():
            raise ToolError(f"not a file: {path}")
        destination.unlink()
        return f"deleted {path}"

    @staticmethod
    def _validate_change(path, contents, size_error):
        if len(contents.encode("utf-8")) > LIMIT:
            raise ToolError(size_error)
        _check_python(path, contents)

    def _fetch_url(self, url):
        try:
            address = urlsplit(url)
            port = address.port
        except ValueError:
            raise Denied("invalid URL or port") from None
        permitted = (
            address.scheme == "https"
            and address.hostname == "docs.python.org"
            and address.username is None and address.password is None
            and port in (None, 443)
            and "?" not in url and "#" not in url
        )
        if not permitted:
            raise Denied("only https://docs.python.org URLs without credentials, "
                         "query, or fragment are allowed")
        else:
            origin = "live docs.python.org"
            try:
                with _open_url(url) as response:
                    mime_type = response.headers.get_content_type()
                    if mime_type != "application/xhtml+xml" and not mime_type.startswith("text/"):
                        raise ToolError(f"unsupported content type: {mime_type}")
                    contents = response.read(LIMIT + 1)
            except urllib.error.HTTPError as error:
                raise ToolError(f"HTTP {error.code} from {url}") from None
            except (urllib.error.URLError, TimeoutError) as error:
                raise ToolError(f"network error: {error}") from None
        return dict(source_url=url, source=origin, truncated=len(contents) > LIMIT,
                    content=contents[:LIMIT].decode("utf-8", errors="replace"))

    def _bash(self, command):
        if self.approve is None or self.approve(command, str(self.root)) is not True:
            raise Denied("bash requires explicit command approval")
        result = self.sandbox.run(command, self.bash_timeout, LIMIT)
        if "stopped" in result:
            raise ToolError(result)
        return result
