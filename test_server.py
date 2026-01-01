import asyncio
import os
import tempfile
import uuid

import pytest
import pytest_asyncio

from server import JuliaSession, SessionManager, TEMP_SESSION_KEY


# -- Helpers --


def make_sentinel() -> str:
    return f"__JULIA_MCP_{uuid.uuid4().hex}__"


@pytest_asyncio.fixture
async def session():
    tmpdir = tempfile.mkdtemp(prefix="julia-mcp-test-")
    s = JuliaSession(tmpdir, make_sentinel(), is_temp=True)
    await s.start()
    yield s
    await s.kill()


@pytest_asyncio.fixture
async def manager():
    m = SessionManager()
    yield m
    await m.shutdown()


# -- JuliaSession tests --


class TestJuliaSession:
    async def test_basic_eval(self, session: JuliaSession):
        result = await session.execute("1 + 1", timeout=30.0)
        assert result == "2"

    async def test_variable_persistence(self, session: JuliaSession):
        await session.execute("x = 42", timeout=30.0)
        result = await session.execute("x + 1", timeout=30.0)
        assert result == "43"

    async def test_println(self, session: JuliaSession):
        result = await session.execute('println("hello world")', timeout=30.0)
        assert result == "hello world"

    async def test_multiline(self, session: JuliaSession):
        code = "function foo(x)\n    x * 2\nend\nfoo(21)"
        result = await session.execute(code, timeout=30.0)
        assert "42" in result

    async def test_multi_expression(self, session: JuliaSession):
        result = await session.execute("a = 1\nb = 2\na + b", timeout=30.0)
        lines = result.strip().splitlines()
        assert lines[-1] == "3"

    async def test_error_handling(self, session: JuliaSession):
        result = await session.execute('error("boom")', timeout=30.0)
        assert "boom" in result
        assert "ERROR" in result or "error" in result.lower()

    async def test_error_does_not_kill_session(self, session: JuliaSession):
        await session.execute('error("boom")', timeout=30.0)
        result = await session.execute("1 + 1", timeout=30.0)
        assert result == "2"

    async def test_nothing_result(self, session: JuliaSession):
        result = await session.execute('println("hi")', timeout=30.0)
        assert "hi" in result

    async def test_large_output(self, session: JuliaSession):
        result = await session.execute("collect(1:100)", timeout=30.0)
        assert "1" in result
        assert "100" in result

    async def test_timeout_kills_session(self, session: JuliaSession):
        with pytest.raises(RuntimeError, match="timed out"):
            await session.execute("sleep(60)", timeout=2.0)
        assert not session.is_alive()

    async def test_is_alive(self, session: JuliaSession):
        assert session.is_alive()

    async def test_kill(self):
        tmpdir = tempfile.mkdtemp(prefix="julia-mcp-test-")
        s = JuliaSession(tmpdir, make_sentinel(), is_temp=True)
        await s.start()
        assert s.is_alive()
        await s.kill()
        assert not s.is_alive()
        assert not os.path.exists(tmpdir)

    async def test_temp_dir_cleanup(self):
        tmpdir = tempfile.mkdtemp(prefix="julia-mcp-test-")
        s = JuliaSession(tmpdir, make_sentinel(), is_temp=True)
        await s.start()
        assert os.path.isdir(tmpdir)
        await s.kill()
        assert not os.path.isdir(tmpdir)

    async def test_non_temp_dir_not_cleaned(self):
        tmpdir = tempfile.mkdtemp(prefix="julia-mcp-test-")
        s = JuliaSession(tmpdir, make_sentinel(), is_temp=False)
        await s.start()
        await s.kill()
        assert os.path.isdir(tmpdir)
        os.rmdir(tmpdir)

    async def test_execute_on_dead_session_raises(self, session: JuliaSession):
        session.process.kill()
        await session.process.wait()
        with pytest.raises(RuntimeError, match="died unexpectedly"):
            await session.execute("1 + 1", timeout=30.0)


# -- SessionManager tests --


class TestSessionManager:
    async def test_lazy_creation(self, manager: SessionManager):
        assert manager.list_sessions() == []
        session = await manager.get_or_create(None)
        assert session.is_alive()
        assert len(manager.list_sessions()) == 1

    async def test_reuse_session(self, manager: SessionManager):
        s1 = await manager.get_or_create(None)
        s2 = await manager.get_or_create(None)
        assert s1 is s2

    async def test_separate_envs(self, manager: SessionManager):
        tmpdir1 = tempfile.mkdtemp(prefix="julia-mcp-test-")
        tmpdir2 = tempfile.mkdtemp(prefix="julia-mcp-test-")
        try:
            s1 = await manager.get_or_create(tmpdir1)
            s2 = await manager.get_or_create(tmpdir2)
            assert s1 is not s2
            assert len(manager.list_sessions()) == 2

            # Variables are isolated
            await s1.execute("x = 100", timeout=30.0)
            result = await s2.execute(
                "try; x; catch; println(\"undefined\"); end", timeout=30.0
            )
            assert "undefined" in result.lower() or "UndefVarError" in result
        finally:
            await manager.shutdown()
            os.rmdir(tmpdir1)
            os.rmdir(tmpdir2)

    async def test_restart(self, manager: SessionManager):
        s1 = await manager.get_or_create(None)
        await s1.execute("x = 42", timeout=30.0)
        await manager.restart(None)
        assert len(manager.list_sessions()) == 0

        s2 = await manager.get_or_create(None)
        assert s2 is not s1
        result = await s2.execute(
            "try; x; catch e; println(e); end", timeout=30.0
        )
        assert "UndefVarError" in result

    async def test_list_sessions(self, manager: SessionManager):
        await manager.get_or_create(None)
        sessions = manager.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]["alive"] is True
        assert sessions[0]["temp"] is True

    async def test_dead_session_auto_recreated(self, manager: SessionManager):
        s1 = await manager.get_or_create(None)
        s1.process.kill()
        await s1.process.wait()
        s2 = await manager.get_or_create(None)
        assert s2 is not s1
        assert s2.is_alive()

    async def test_shutdown_cleans_all(self, manager: SessionManager):
        tmpdir = tempfile.mkdtemp(prefix="julia-mcp-test-")
        await manager.get_or_create(None)
        await manager.get_or_create(tmpdir)
        assert len(manager.list_sessions()) == 2
        await manager.shutdown()
        assert len(manager.list_sessions()) == 0
        # Non-temp dir still exists
        assert os.path.isdir(tmpdir)
        os.rmdir(tmpdir)


# -- Timeout auto-detection tests --


class TestTimeoutDetection:
    """Test that PKG_PATTERN correctly identifies Pkg/using/import code."""

    from server import PKG_PATTERN

    @pytest.mark.parametrize(
        "code",
        [
            "using LinearAlgebra",
            "import Pkg",
            "Pkg.add(\"Example\")",
            "using Pkg; Pkg.status()",
        ],
    )
    def test_pkg_pattern_matches(self, code: str):
        assert self.PKG_PATTERN.search(code)

    @pytest.mark.parametrize(
        "code",
        [
            "1 + 1",
            "x = 42",
            "let x = 1; end",
            "f(x) = x^2",
        ],
    )
    def test_pkg_pattern_no_match(self, code: str):
        assert not self.PKG_PATTERN.search(code)
