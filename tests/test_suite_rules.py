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
process to the next. CI runs the suite twice, the second time last to first (``--reverse-order``,
``tests/conftest.py``) under another ``PYTHONHASHSEED``, and compares the two runs with
``scripts/check_junit.py --same-as``: that shows a dependence on run order or on the seed only where
it changed an outcome between those two runs. The same scan over the package finds its reads only in
its two real clocks, and over ``alembic/`` and ``scripts/``, which the suite runs in process, finds
none;
at run time ``tests/determinism.py`` refuses both clocks, network connections other than Unix
sockets, and the ``socket`` module's host-name lookups, in every test.

No test function or test class is defined inside a function, or inside a class whose name does not
start with ``Test``, where pytest never collects it. Out of scope: a module pytest's file pattern
does not match, a ``Test`` class with an ``__init__``, and pytest's naming options changed in its
configuration.
"""

from __future__ import annotations

import ast
import asyncio
import socket
from datetime import UTC, datetime, tzinfo
from pathlib import Path

import pytest

from github_activity_db.db import MIGRATION_REVISIONS
from github_activity_db.db.types import utc_now
from github_activity_db.source import GitHubSource, clock
from github_activity_db.source.clock import SystemClock
from tests import determinism
from tests.determinism import NotDeterministic
from tests.pytest_runs import load_check_junit, run_pytest
from tests.source.conftest import FakeClock
from tests.support import files_under

TESTS = Path(__file__).resolve().parent
SRC = TESTS.parent / "src" / "github_activity_db"
ALEMBIC = TESTS.parent / "alembic"
SCRIPTS = TESTS.parent / "scripts"
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
    if isinstance(seed, ast.Starred):  # ``*args``, which the scan cannot read
        seed = None
    for keyword in call.keywords:
        if keyword.arg in ("x", None):  # None: ``**kwargs``, which the scan cannot read
            seed = keyword.value if keyword.arg == "x" else None
    return seed is not None and not (isinstance(seed, ast.Constant) and seed.value is None)


def uncollected_tests(*paths: Path) -> list[str]:
    """Test functions and classes defined inside a function, or inside a class whose name does not
    start with ``Test``, where pytest never collects them; each is listed once for every such
    function or class around it."""
    found = []
    for path in paths:
        where = path.relative_to(path.parents[1])
        for outer in ast.walk(ast.parse(path.read_bytes())):
            if not (
                isinstance(outer, ast.FunctionDef | ast.AsyncFunctionDef)
                or (isinstance(outer, ast.ClassDef) and not outer.name.startswith("Test"))
            ):
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
    assert len(files) == len(files_under(root, {".py"}))
    assert files, f"no Python file under {root}"
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
        "db/types.py:16: datetime.now",
        "source/clock.py:32: secrets.SystemRandom",
        "source/clock.py:35: datetime.now",
        "source/clock.py:38: asyncio.sleep",
    ]


def test_the_migrations_read_no_wall_clock_or_randomness() -> None:
    """Over ``alembic/env.py`` and one file for each revision of the chain, which every database
    test runs in process, the scan finds nothing."""
    files = _python_files(ALEMBIC)
    names = [path.relative_to(ALEMBIC).as_posix() for path in files]
    assert names[0] == "env.py"
    assert sorted(name.removeprefix("versions/").split("_")[0] for name in names[1:]) == sorted(
        MIGRATION_REVISIONS
    )
    assert len(files) == 1 + len(MIGRATION_REVISIONS)
    assert determinism_problems(*files) == []


def test_the_scripts_read_no_wall_clock_or_randomness() -> None:
    """Over ``scripts/``, whose checks the suite also runs in process, the scan finds nothing."""
    files = _python_files(SCRIPTS)
    assert [path.name for path in files] == sorted(
        path.name for path in SCRIPTS.iterdir() if path.suffix == ".py"
    )
    assert {"check_junit.py", "count_suppressions.py", "record_fixtures.py"} <= {
        path.name for path in files
    }
    assert determinism_problems(*files) == []


def test_no_test_is_defined_inside_a_function_or_a_non_test_class() -> None:
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


@pytest.mark.parametrize(("method", "returned"), [("connect", None), ("connect_ex", 0)])
def test_a_connection_to_a_unix_socket_goes_through(
    method: str, returned: int | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A relative path, since macOS limits a Unix socket's path to 104 bytes.
    monkeypatch.chdir(tmp_path)
    with socket.socket(socket.AF_UNIX) as listening, socket.socket(socket.AF_UNIX) as connection:
        listening.bind("s")
        listening.listen(1)
        assert getattr(connection, method)("s") == returned
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


def test_a_lookup_by_keyword_is_refused() -> None:
    with pytest.raises(NotDeterministic) as refusal:
        socket.getaddrinfo(host="api.github.com", port=443)
    assert _refused(refusal) == ("network", "getaddrinfo {'host': 'api.github.com', 'port': 443}")


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
    assert _refused(refusal) == ("wall clock", "source.clock: datetime.now(UTC)")


@pytest.mark.parametrize("reading", ["utcnow", "today"])
def test_the_real_clocks_other_readings_are_refused(reading: str) -> None:
    """The datetime source/clock.py sees refuses each way to read the time, not only ``now``."""
    with pytest.raises(NotDeterministic) as refusal:
        getattr(vars(clock)["datetime"], reading)()
    assert _refused(refusal) == ("wall clock", f"source.clock: datetime.{reading}()")


def test_the_real_clocks_asyncio_answers_everything_but_sleep() -> None:
    """The asyncio source/clock.py sees is asyncio for every name but ``sleep``."""
    seen = vars(clock)["asyncio"]
    assert seen.get_running_loop is asyncio.get_running_loop
    assert seen.sleep.__qualname__ == "_NoSleep.sleep"
    assert determinism.REFUSED == []


async def test_the_real_clocks_sleep_is_refused() -> None:
    with pytest.raises(NotDeterministic) as refusal:
        await SystemClock().sleep(2.0)
    assert _refused(refusal) == ("wall clock", "source.clock: asyncio.sleep(2.0)")


def test_the_real_clocks_unseeded_jitter_is_refused() -> None:
    with pytest.raises(NotDeterministic) as refusal:
        SystemClock().jitter(8.0)
    assert _refused(refusal) == ("randomness", "SystemClock._random.uniform(0.0, 2.0)")


@pytest.mark.parametrize(
    ("method", "args", "kwargs", "detail"),
    [
        ("random", (), {}, "random()"),
        ("getrandbits", (8,), {}, "getrandbits(8)"),
        ("randbytes", (4,), {}, "randbytes(4)"),
        ("choice", (["a", "b"],), {}, "choice(['a', 'b'])"),
        ("gauss", (), {"mu": 0.0, "sigma": 1.0}, "gauss(mu=0.0, sigma=1.0)"),
    ],
    ids=["random", "getrandbits", "randbytes", "choice", "gauss"],
)
def test_every_draw_from_the_real_clocks_random_source_is_refused(
    method: str, args: tuple[object, ...], kwargs: dict[str, object], detail: str
) -> None:
    with pytest.raises(NotDeterministic) as refusal:
        getattr(SystemClock._random, method)(*args, **kwargs)
    assert _refused(refusal) == ("randomness", f"SystemClock._random.{detail}")


def test_the_real_clocks_random_source_answers_no_dunder_name() -> None:
    """``copy`` and ``pickle`` look up dunder names on an object; none is a draw."""
    assert not hasattr(SystemClock._random, "__deepcopy__")
    assert determinism.REFUSED == []


def test_the_column_defaults_clock_is_refused() -> None:
    with pytest.raises(NotDeterministic) as refusal:
        utc_now()
    assert _refused(refusal) == ("wall clock", "db.types: datetime.now(UTC)")


# A fixed instant for the real clock's tests.
INSTANT = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=UTC)


def test_the_real_clock_reads_the_time_in_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    """``SystemClock.now`` over a fixed ``datetime``: INSTANT in the zone it asks for, or naive
    when it asks for none."""

    class Fixed(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Fixed:
            moment = INSTANT.astimezone(tz) if tz is not None else INSTANT.replace(tzinfo=None)
            return cls.combine(moment.date(), moment.timetz())

    monkeypatch.setattr(clock, "datetime", Fixed)
    now = SystemClock().now()
    assert (now, now.tzinfo) == (INSTANT, UTC)


async def test_the_real_clock_sleeps_the_seconds_it_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slept: list[float] = []

    class Recording:
        @staticmethod
        async def sleep(seconds: float) -> None:
            slept.append(seconds)

    monkeypatch.setattr(clock, "asyncio", Recording)
    await SystemClock().sleep(2.5)
    assert slept == [2.5]


def _pytest_over(
    root: Path, files: dict[str, str], *options: str
) -> tuple[int, list[tuple[str, str]]]:
    """pytest run over ``files``, written to ``root`` beside a configuration of their own, with this
    repository importable: its exit code, and each test's outcome in the order its JUnit report
    lists the tests, which is the order they ran in."""
    run = run_pytest(root, files, *options, importable=True)
    report = root / "junit.xml"
    assert report.is_file(), run.stdout + run.stderr
    return run.returncode, list(load_check_junit().read_outcomes(report).items())


# Catches the refusal of the real clock, as code under test may.
SWALLOW = (
    "from github_activity_db.source.clock import SystemClock\n\n\n"
    "def swallow():\n"
    "    try:\n"
    "        SystemClock().now()\n"
    "    except BaseException:\n"
    "        pass\n"
)


def test_a_refusal_the_code_caught_fails_the_next_check(tmp_path: Path) -> None:
    """Sample tests under this conftest's guard and checks: each refusal caught where it was
    raised fails the first check after it, the one after each test or the one at the end of the
    session, and the next check passes again."""
    files = {
        "conftest.py": (
            "from tests.conftest import (\n"
            "    nothing_refused,\n"
            "    nothing_refused_after_the_last_test,\n"
            "    pytest_configure,\n"
            "    pytest_unconfigure,\n"
            ")\n"
        ),
        "swallow.py": SWALLOW,
        "test_a_collection.py": "from swallow import swallow\n\nswallow()\n\n\n"
        "def test_first():\n    pass\n",
        "test_b_test.py": "from swallow import swallow\n\n\n"
        "def test_swallows():\n    swallow()\n\n\n"
        "def test_after_it():\n    pass\n",
        "test_c_module.py": "import pytest\nfrom swallow import swallow\n\n\n"
        "@pytest.fixture(scope='module')\ndef at_setup():\n    swallow()\n\n\n"
        "@pytest.fixture(scope='module')\ndef at_teardown():\n    yield\n    swallow()\n\n\n"
        "def test_with_a_setup_that_swallows(at_setup):\n    pass\n\n\n"
        "def test_with_a_teardown_that_swallows(at_teardown):\n    pass\n",
        "test_d_next.py": "def test_after_the_module_teardown():\n    pass\n",
        "test_e_last.py": "import pytest\nfrom swallow import swallow\n\n\n"
        "@pytest.fixture(scope='session')\ndef at_the_end():\n    yield\n    swallow()\n\n\n"
        "def test_last(at_the_end):\n    pass\n",
    }
    assert _pytest_over(tmp_path / "run", files) == (
        1,
        [
            ("test_a_collection::test_first", "error"),
            ("test_b_test::test_swallows", "error"),
            ("test_b_test::test_after_it", "passed"),
            ("test_c_module::test_with_a_setup_that_swallows", "error"),
            ("test_c_module::test_with_a_teardown_that_swallows", "passed"),
            ("test_d_next::test_after_the_module_teardown", "error"),
            ("test_e_last::test_last", "error"),
        ],
    )


def test_reverse_order_runs_the_tests_last_to_first(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two runs of sample tests, the second with ``--reverse-order``, compared as CI compares its
    two runs: test_b passes only after test_a, so only test_b differs."""
    files = {
        "conftest.py": (
            "from tests.conftest import pytest_addoption, pytest_collection_modifyitems\n"
        ),
        "test_one.py": "SEEN = []\n\n\ndef test_a():\n    SEEN.append('a')\n\n\n"
        "def test_b():\n    assert SEEN == ['a']\n",
        "test_two.py": "def test_c():\n    pass\n",
    }
    assert _pytest_over(tmp_path / "forward", files) == (
        0,
        [
            ("test_one::test_a", "passed"),
            ("test_one::test_b", "passed"),
            ("test_two::test_c", "passed"),
        ],
    )
    assert _pytest_over(tmp_path / "backward", files, "--reverse-order") == (
        1,
        [
            ("test_two::test_c", "passed"),
            ("test_one::test_b", "failed"),
            ("test_one::test_a", "passed"),
        ],
    )
    forward, backward = (str(tmp_path / run / "junit.xml") for run in ("forward", "backward"))
    assert load_check_junit().main([forward, "--max-skipped", "0", "--same-as", backward]) == 1
    assert capsys.readouterr() == (
        "{'tests': 3, 'failures': 0, 'errors': 0, 'skipped': 0}\n",
        "FAIL: differs: test_one::test_b: passed vs failed\n",
    )


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("import subprocess\nsubprocess.run(['true'])\n", True),
        ("import subprocess\nsubprocess.check_output(['true'], text=True)\n", True),
        ("import subprocess\nsubprocess.run(['true'], timeout=None)\n", True),
        ("import subprocess\nsubprocess.run(['true'], **options)\n", True),
        ("from subprocess import run\nrun(['true'])\n", True),
        ("from subprocess import run as go\ngo(['true'])\n", True),
        ("from subprocess import run\nwait = run\n", True),
        ("import subprocess as sp\nsp.run(['true'], timeout=60)\n", True),
        ("from subprocess import *\nrun(['true'], timeout=60)\n", True),
        ("from subprocess import Popen\n", True),
        ("import subprocess\nsubprocess.Popen(['true']).communicate()\n", True),
        ("import subprocess\nsubprocess.getoutput('true')\n", True),
        ("import subprocess\nmodule = subprocess\n", True),
        ("import subprocess\nsubprocess.run(['true'], timeout=60)\n", False),
        ("from subprocess import run\nrun(['true'], timeout=60)\n", False),
        (
            "import subprocess\n"
            "def f() -> subprocess.CompletedProcess[str]:\n"
            "    try:\n"
            "        return subprocess.run(['true'], timeout=60, stdout=subprocess.PIPE)\n"
            "    except subprocess.CalledProcessError:\n"
            "        raise\n",
            False,
        ),
        ("def run(argv):\n    return argv\nrun(['true'])\n", False),
    ],
)
def test_flags_each_shape_it_does_not_model(tmp_path: Path, source: str, flagged: bool) -> None:
    (tmp_path / "pkg").mkdir()
    path = tmp_path / "pkg" / "module.py"
    path.write_text(source)
    assert bool(subprocess_problems(path)) is flagged


def _module(tmp_path: Path, source: str) -> Path:
    (tmp_path / "pkg").mkdir()
    path = tmp_path / "pkg" / "module.py"
    path.write_text(source)
    return path


@pytest.mark.parametrize(
    ("source", "problems"),
    [
        ("from datetime import datetime\nnow = datetime.now()\n", ["2: datetime.now"]),
        ("import datetime\nnow = datetime.datetime.utcnow()\n", ["2: datetime.datetime.utcnow"]),
        ("from datetime import date\ntoday = date.today()\n", ["2: date.today"]),
        ("from datetime import datetime as dt\n", ["1: datetime.datetime imported as dt"]),
        ("import time\nstarted = time.monotonic()\n", ["2: time.monotonic"]),
        ("from time import sleep\n", ["1: imports time.sleep"]),
        ("import time as clock\n", ["1: time imported as clock"]),
        ("import time\nclock = time\n", ["2: the time module used as a value"]),
        ("import random\nvalue = random.random()\n", ["2: random.random"]),
        ("import random\nrng = random.Random()\n", ["2: random.Random without a seed"]),
        ("from random import Random\nrng = Random(None)\n", ["2: Random without a seed"]),
        ("import secrets\ntoken = secrets.token_hex()\n", ["2: secrets.token_hex"]),
        ("import asyncio\n\n\nasync def f():\n    await asyncio.sleep(1)\n", ["5: asyncio.sleep"]),
        ("from asyncio import sleep\n", ["1: imports asyncio.sleep"]),
        ("import os.path\nkey = os.urandom(8)\n", ["2: os.urandom"]),
        ("import uuid\nkey = uuid.uuid4()\n", ["2: uuid.uuid4"]),
        (
            "import time\nfrom typing import Annotated\n\n\n"
            "def f(x: Annotated[float, time.time] = 0.0) -> None:\n    pass\n",
            ["5: time.time"],
        ),
        ("from time import *\n", ["1: imports time.*"]),
        ("from random import *\n", ["1: imports random.*"]),
        ("from secrets import *\n", ["1: imports secrets.*"]),
        ("from asyncio import *\n", ["1: imports asyncio.*"]),
        ("from os import *\n", ["1: imports os.*"]),
        ("from uuid import *\n", ["1: imports uuid.*"]),
        ("from datetime import *\n", ["1: imports datetime.*"]),
        ("import time\n\n\ndef f(x: g(time.time()) = 0) -> None:\n    pass\n", ["4: time.time"]),
        ("import time\n\n\ndef f() -> g(time.time()):\n    pass\n", ["4: time.time"]),
        ("import time\nx: g(time.time()) = 0\n", ["2: time.time"]),
        (
            "import random\nseeds = []\nrng = random.Random(*seeds)\n",
            ["3: random.Random without a seed"],
        ),
        ("from random import Random\nrng = Random(*())\n", ["2: Random without a seed"]),
        ("import random\nrng = random.Random(**{})\n", ["2: random.Random without a seed"]),
        ("import time\ntime.tzset()\n", []),
        ("import time\n\n\ndef f(x: time.struct_time) -> None:\n    pass\n", []),
        ("import random\nrng = random.Random(7)\n", []),
        ("import random\nrng = random.Random(x=7)\n", []),
        ("import random\n\n\ndef f(rng: random.Random) -> None:\n    pass\n", []),
        ("import asyncio\n\n\nasync def f():\n    await asyncio.sleep(0)\n", []),
        ("import uuid\nkey = uuid.uuid5(uuid.NAMESPACE_URL, 'x')\n", []),
        ("from datetime import UTC, datetime\nwhen = datetime(2026, 1, 1, tzinfo=UTC)\n", []),
        ("clock = make_clock()\nclock.now()\n", []),
    ],
)
def test_the_determinism_scan_reports_each_spelling(
    tmp_path: Path, source: str, problems: list[str]
) -> None:
    assert determinism_problems(_module(tmp_path, source)) == [
        f"pkg/module.py:{problem}" for problem in problems
    ]


@pytest.mark.parametrize(
    ("source", "problems"),
    [
        (
            "def test_outer():\n    def test_inner():\n        pass\n",
            ["2: test_inner inside test_outer"],
        ),
        ("def helper():\n    class TestNested:\n        pass\n", ["2: TestNested inside helper"]),
        (
            "class Helper:\n    def test_never_collected(self):\n        pass\n",
            ["2: test_never_collected inside Helper"],
        ),
        (
            "class Helper:\n    class TestInner:\n        def test_x(self):\n            pass\n",
            ["2: TestInner inside Helper", "3: test_x inside Helper"],
        ),
        ("def test_a():\n    pass\n\n\nclass TestB:\n    def test_c(self):\n        pass\n", []),
        (
            "class TestOuter:\n    class TestInner:\n        def test_x(self):\n            pass\n",
            [],
        ),
        ("class Helper:\n    def check(self):\n        pass\n", []),
    ],
)
def test_the_collection_scan_reports_each_nested_test(
    tmp_path: Path, source: str, problems: list[str]
) -> None:
    assert uncollected_tests(_module(tmp_path, source)) == [
        f"pkg/module.py:{problem}" for problem in problems
    ]
