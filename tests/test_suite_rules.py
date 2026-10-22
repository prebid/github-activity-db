"""Rules the test suite holds itself to (docs/testing.md, Principles).

Each wait on a subprocess has an explicit timeout (redesign plan Step 3). The scan models the
``subprocess`` module imported under its own name: ``subprocess.run``, ``call``,
``check_call``, and ``check_output``, called as attributes of the module or by the names
``from subprocess import ...`` binds, pass a ``timeout`` that is not ``None``, and its exception
and constant names start nothing. Every other use of the module is refused rather than read: an
alias, a star import, ``Popen``, ``getoutput``, or the module or a waiting function passed as a
value. Out of scope: the module reached without an import statement (``importlib.import_module``,
``__import__``, ``sys.modules``), and process APIs outside ``subprocess`` (``os.system``,
``os.popen``, ``asyncio``'s subprocesses, ``multiprocessing``).

No test file reads the wall clock or draws unseeded random numbers (D40). The scan models these
modules imported under their own names: ``time`` is used only for ``tzset``, ``random`` only for
``Random`` called with a seed, and ``secrets`` not at all; ``datetime.now``, ``utcnow``, and
``today`` and ``date.today`` are not read, through the module or the classes ``from datetime
import ...`` binds; ``asyncio.sleep`` is called only with ``0``; and ``os.urandom``,
``os.getrandom``, ``uuid.uuid1``, and ``uuid.uuid4`` are not used. ``Random(None)`` draws its seed
from the OS and counts as unseeded. An alias or a star import of these modules or names is refused
rather than read, and so are ``time``, ``random``, ``secrets``, and the names listed passed as
values; ``asyncio``, ``os``, ``uuid``, and ``datetime`` are read only through the names listed, so
one of them passed as a value (to ``getattr``, say) is out of scope. A name in an annotation is a
type and is not read, unless the annotation holds a call or ``Annotated`` metadata, which code reads
at run time. Out of scope as well: the modules reached without an import statement, the event loop's
``time()``, file times, and the order of a set of strings, which ``PYTHONHASHSEED`` changes from one
process to the next (two runs compared by ``scripts/check_junit.py --same-as`` show that). The same
scan over the package finds its reads only in its two real clocks; at run time
``tests/determinism.py`` refuses one of them, ``SystemClock``, and the network, in every test.

Every test function is one pytest collects: none is defined inside another function, where it
would never run.
"""

from __future__ import annotations

import ast
import os
import socket
from pathlib import Path

import pytest

from github_activity_db.source import GitHubSource
from github_activity_db.source.clock import SystemClock
from tests import determinism
from tests.determinism import NotDeterministic
from tests.source.conftest import FakeClock

TESTS = Path(__file__).resolve().parent
SRC = TESTS.parent / "src" / "github_activity_db"
WAITING = frozenset({"run", "call", "check_call", "check_output"})
# subprocess names that start no process.
INERT = frozenset(
    {
        "CalledProcessError",
        "CompletedProcess",
        "DEVNULL",
        "PIPE",
        "STDOUT",
        "SubprocessError",
        "TimeoutExpired",
    }
)


def _timed(call: ast.Call) -> bool:
    return any(
        keyword.arg == "timeout"
        and not (isinstance(keyword.value, ast.Constant) and keyword.value.value is None)
        for keyword in call.keywords
    )


def subprocess_problems(*paths: Path) -> list[str]:
    found = []
    for path in paths:
        tree = ast.parse(path.read_bytes())
        nodes = list(ast.walk(tree))
        where = path.relative_to(path.parents[1])
        waits: set[str] = set()
        for node in nodes:
            if isinstance(node, ast.Import):
                found += [
                    f"{where}:{node.lineno}: subprocess imported as {alias.asname}"
                    for alias in node.names
                    if alias.name == "subprocess" and alias.asname not in (None, "subprocess")
                ]
            elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
                for alias in node.names:
                    if alias.name in WAITING:
                        waits.add(alias.asname or alias.name)
                    elif alias.name not in INERT:
                        found.append(f"{where}:{node.lineno}: imports subprocess.{alias.name}")
        called = {id(node.func): node for node in nodes if isinstance(node, ast.Call)}
        bases = {id(node.value) for node in nodes if isinstance(node, ast.Attribute)}
        for node in nodes:
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "subprocess"
            ):
                if node.attr in INERT:
                    continue
                if node.attr not in WAITING or id(node) not in called:
                    found.append(f"{where}:{node.lineno}: subprocess.{node.attr}")
                elif not _timed(called[id(node)]):
                    found.append(f"{where}:{node.lineno}: no timeout")
            elif isinstance(node, ast.Name) and node.id == "subprocess" and id(node) not in bases:
                found.append(f"{where}:{node.lineno}: the subprocess module used as a value")
            elif isinstance(node, ast.Name) and node.id in waits:
                if id(node) not in called:
                    found.append(f"{where}:{node.lineno}: {node.id} used as a value")
                elif not _timed(called[id(node)]):
                    found.append(f"{where}:{node.lineno}: no timeout")
    return found


# Modules a test file uses only through these names; the rest of each reads the wall clock, sleeps,
# or draws unseeded random numbers.
ONLY = {
    "time": frozenset({"tzset"}),
    "random": frozenset({"Random"}),
    "secrets": frozenset(),
}
# Names in other modules that read the wall clock, sleep, or draw unseeded random numbers.
NEVER = {
    "asyncio": frozenset({"sleep"}),
    "os": frozenset({"urandom", "getrandom"}),
    "uuid": frozenset({"uuid1", "uuid4"}),
}
# The datetime classes' methods that read the wall clock.
NOW = {"datetime": frozenset({"now", "utcnow", "today"}), "date": frozenset({"today"})}


def _sleeps_zero(call: ast.Call) -> bool:
    return (
        len(call.args) == 1
        and not call.keywords
        and isinstance(call.args[0], ast.Constant)
        and call.args[0].value == 0
    )


def determinism_problems(*paths: Path) -> list[str]:
    found = []
    for path in paths:
        tree = ast.parse(path.read_bytes())
        nodes = list(ast.walk(tree))
        where = path.relative_to(path.parents[1])
        modules: set[str] = set()
        bound: dict[str, str] = {}  # a name ``from random import Random`` binds, to its module
        classes: set[str] = set()  # the datetime classes ``from datetime import ...`` binds
        for node in nodes:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in {*ONLY, *NEVER, "datetime"}:
                        if alias.asname not in (None, alias.name):
                            found.append(
                                f"{where}:{node.lineno}: {alias.name} imported as {alias.asname}"
                            )
                        modules.add(alias.name)
                    elif alias.asname is None and alias.name.split(".")[0] in {*ONLY, *NEVER}:
                        # ``import os.path`` binds ``os``.
                        modules.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module in {*ONLY, *NEVER, "datetime"}:
                for alias in node.names:
                    name = f"{node.module}.{alias.name}"
                    if alias.asname not in (None, alias.name):
                        found.append(f"{where}:{node.lineno}: {name} imported as {alias.asname}")
                    elif node.module in ONLY and alias.name not in ONLY[node.module]:
                        found.append(f"{where}:{node.lineno}: imports {name}")
                    elif node.module in NEVER and alias.name in NEVER[node.module] | {"*"}:
                        found.append(f"{where}:{node.lineno}: imports {name}")
                    elif node.module == "datetime" and alias.name == "*":
                        found.append(f"{where}:{node.lineno}: imports {name}")
                    elif node.module in ONLY:
                        bound[alias.name] = node.module
                    elif node.module == "datetime" and alias.name in NOW:
                        classes.add(alias.name)
        called = {id(node.func): node for node in nodes if isinstance(node, ast.Call)}
        bases = {id(node.value) for node in nodes if isinstance(node, ast.Attribute)}
        typed = _types(nodes)
        for node in nodes:
            if id(node) in typed:
                continue
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                module, attr = node.value.id, node.attr
                if module in modules and module in ONLY:
                    if attr not in ONLY[module]:
                        found.append(f"{where}:{node.lineno}: {module}.{attr}")
                    elif module == "random" and not _seeded(called.get(id(node))):
                        found.append(f"{where}:{node.lineno}: random.Random without a seed")
                elif module in modules and attr in NEVER.get(module, ()):
                    call = called.get(id(node))
                    if not (module == "asyncio" and call is not None and _sleeps_zero(call)):
                        found.append(f"{where}:{node.lineno}: {module}.{attr}")
                elif module in classes and attr in NOW[module]:
                    found.append(f"{where}:{node.lineno}: {module}.{attr}")
            elif (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Attribute)
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id == "datetime"
                and "datetime" in modules
                and node.value.attr in NOW
                and node.attr in NOW[node.value.attr]
            ):
                found.append(f"{where}:{node.lineno}: datetime.{node.value.attr}.{node.attr}")
            elif isinstance(node, ast.Name) and id(node) not in bases:
                if node.id in modules and node.id in ONLY:
                    found.append(f"{where}:{node.lineno}: the {node.id} module used as a value")
                elif bound.get(node.id) == "random" and not _seeded(called.get(id(node))):
                    found.append(f"{where}:{node.lineno}: Random without a seed")
    return found


def _types(nodes: list[ast.AST]) -> set[int]:
    """The ids of the nodes in annotations that only name types. An annotation holding a call or
    ``Annotated`` metadata is left out: code reads those at run time."""
    annotations: list[ast.expr | None] = []
    for node in nodes:
        if isinstance(node, ast.arg | ast.AnnAssign):
            annotations.append(node.annotation)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            annotations.append(node.returns)
    typed: set[int] = set()
    for annotation in annotations:
        parts = list(ast.walk(annotation)) if annotation is not None else []
        if not any(
            isinstance(part, ast.Call)
            or (isinstance(part, ast.Name) and part.id == "Annotated")
            or (isinstance(part, ast.Attribute) and part.attr == "Annotated")
            for part in parts
        ):
            typed.update(id(part) for part in parts)
    return typed


def _seeded(call: ast.Call | None) -> bool:
    """``Random`` called with a seed: its first argument, or ``x``, given and not ``None``."""
    if call is None:
        return False
    seed = call.args[0] if call.args else None
    for keyword in call.keywords:
        if keyword.arg in ("x", None):  # None: ``**kwargs``, which the scan cannot read
            seed = keyword.value if keyword.arg == "x" else None
    return seed is not None and not (isinstance(seed, ast.Constant) and seed.value is None)


def uncollected_tests(*paths: Path) -> list[str]:
    """Test functions and classes defined inside a function, where pytest never collects them."""
    found = []
    for path in paths:
        where = path.relative_to(path.parents[1])
        for outer in ast.walk(ast.parse(path.read_bytes())):
            if not isinstance(outer, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(outer):
                if node is not outer and (
                    (
                        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                        and node.name.startswith("test")
                    )
                    or (isinstance(node, ast.ClassDef) and node.name.startswith("Test"))
                ):
                    found.append(f"{where}:{node.lineno}: {node.name} inside {outer.name}")
    return found


def _python_files(root: Path) -> list[Path]:
    files = sorted(root.rglob("*.py"))
    # Counted with os.walk rather than the glob, so a scan cannot pass on fewer files.
    assert len(files) == sum(
        Path(name).suffix == ".py" for _, _, names in os.walk(root) for name in names
    )
    return files


def _test_files() -> list[Path]:
    return _python_files(TESTS)


def test_every_subprocess_wait_in_the_suite_has_a_timeout() -> None:
    assert subprocess_problems(*_test_files()) == []


def test_no_test_file_reads_the_wall_clock_or_draws_unseeded_random_numbers() -> None:
    assert determinism_problems(*_test_files()) == []


def test_the_package_reads_the_wall_clock_and_randomness_only_in_its_real_clocks() -> None:
    """Over every module of the package, the scan finds the reads of the two real clocks, and no
    other: everything else takes a clock it is given, which a test fixes."""
    assert determinism_problems(*_python_files(SRC)) == [
        "db/types.py:15: datetime.now",
        "source/clock.py:32: secrets.SystemRandom",
        "source/clock.py:35: datetime.now",
        "source/clock.py:38: asyncio.sleep",
    ]


def test_pytest_collects_every_test_function_in_the_suite() -> None:
    assert uncollected_tests(*_test_files()) == []


def _refused(refusal: pytest.ExceptionInfo[NotDeterministic]) -> tuple[str, str]:
    """The refusal's fields, once it is the only one recorded; the record is then cleared, so the
    conftest's check after the test passes."""
    assert determinism.REFUSED == [refusal.value]
    determinism.REFUSED.clear()
    return refusal.value.what, refusal.value.detail


@pytest.mark.parametrize("method", ["connect", "connect_ex"])
def test_a_connection_to_a_host_is_refused(method: str) -> None:
    with pytest.raises(NotDeterministic) as refusal, socket.socket() as connection:
        getattr(connection, method)(("192.0.2.1", 443))
    assert _refused(refusal) == ("network", f"{method} ('192.0.2.1', 443)")


@pytest.mark.parametrize("method", ["connect", "connect_ex"])
def test_a_connection_to_a_unix_socket_goes_through(
    method: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A relative path, since macOS limits a Unix socket's path to 104 bytes.
    monkeypatch.chdir(tmp_path)
    with socket.socket(socket.AF_UNIX) as listening, socket.socket(socket.AF_UNIX) as connection:
        listening.bind("s")
        listening.listen(1)
        assert getattr(connection, method)("s") in (None, 0)
        assert connection.getpeername() == "s"
    assert determinism.REFUSED == []


@pytest.mark.parametrize(
    ("lookup", "argument", "detail"),
    [
        ("getaddrinfo", ("api.github.com", 443), "getaddrinfo 'api.github.com'"),
        ("gethostbyname", ("api.github.com",), "gethostbyname 'api.github.com'"),
        ("gethostbyname_ex", ("api.github.com",), "gethostbyname_ex 'api.github.com'"),
        ("gethostbyaddr", ("192.0.2.1",), "gethostbyaddr '192.0.2.1'"),
        ("getnameinfo", (("192.0.2.1", 443), 0), "getnameinfo ('192.0.2.1', 443)"),
        ("getfqdn", ("api.github.com",), "gethostbyaddr 'api.github.com'"),
    ],
    ids=[*determinism.LOOKUPS, "getfqdn"],
)
def test_every_host_name_lookup_is_refused(
    lookup: str, argument: tuple[object, ...], detail: str
) -> None:
    with pytest.raises(NotDeterministic) as refusal:
        getattr(socket, lookup)(*argument)
    assert _refused(refusal) == ("network", detail)


@pytest.mark.parametrize(
    "proxy",
    [
        {},
        {"HTTPS_PROXY": "http://192.0.2.10:3128"},
        {"https_proxy": "http://192.0.2.10:3128"},
        {"ALL_PROXY": "http://192.0.2.10:3128"},
    ],
    ids=["none", "HTTPS_PROXY", "https_proxy", "ALL_PROXY"],
)
async def test_the_source_without_a_recorded_transport_is_refused_the_network(
    proxy: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The source as a command builds it, with githubkit's own transport: its first request is
    refused at the lookup of GitHub's host, whatever proxy the environment names."""
    for name, value in proxy.items():
        monkeypatch.setenv(name, value)
    async with GitHubSource("test-token", clock=FakeClock()) as source:
        with pytest.raises(NotDeterministic) as refusal:
            await source.resolve("prebid", "Prebid.js")
    assert _refused(refusal) == ("network", "getaddrinfo b'api.github.com'")


def test_the_real_clock_is_refused() -> None:
    with pytest.raises(NotDeterministic) as refusal:
        SystemClock().now()
    assert _refused(refusal) == ("wall clock", "SystemClock.now()")


async def test_the_real_clocks_sleep_is_refused() -> None:
    with pytest.raises(NotDeterministic) as refusal:
        await SystemClock().sleep(2.0)
    assert _refused(refusal) == ("wall clock", "SystemClock.sleep(2.0)")


def test_the_real_clocks_unseeded_jitter_is_refused() -> None:
    with pytest.raises(NotDeterministic) as refusal:
        SystemClock().jitter(8.0)
    assert _refused(refusal) == ("randomness", "SystemClock._random.uniform(0.0, 2.0)")
