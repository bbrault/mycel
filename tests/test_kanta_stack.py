"""Tests for the kanta-stack provisioning wrapper (kanta_stack.py).

The live `up` path runs docker/composer/migrate and mutates real repos, so it is
never exercised here. We cover the pure logic (slug, env parsing, path mapping,
prerequisites) and the command paths against a fake `kanta-stack` shell script.
"""
from __future__ import annotations

import os
import stat
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kanta_stack import KantaStack, parse_env_file, slugify_task


class TestSlugifyTask:
    def test_extracts_jira_id_lowercased(self) -> None:
        assert slugify_task("Fix KAN-341 login bug", "dev-1") == "kan-341"

    def test_jira_id_from_url(self) -> None:
        assert slugify_task("https://jira/browse/KAN-99", "dev-1") == "kan-99"

    def test_free_text_slugified(self) -> None:
        assert slugify_task("Add the export feature!", "dev-1") == "add-the-export-feature"

    def test_empty_uses_fallback(self) -> None:
        assert slugify_task("", "dev-7") == "dev-7"

    def test_reserved_default_uses_fallback(self) -> None:
        assert slugify_task("default", "dev-2") == "dev-2"

    def test_fallback_also_sanitized(self) -> None:
        assert slugify_task("", "Dev Run #3") == "dev-run-3"


class TestParseEnvFile:
    def test_parses_key_values_and_skips_comments(self) -> None:
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "x.env")
        with open(path, "w") as fh:
            fh.write("# comment\nKANTA_LAB_PORT=8180\nKANTA_LAB_API_V2_PATH=\"/p/kanta-api-v2\"\n\n")
        env = parse_env_file(path)
        assert env["KANTA_LAB_PORT"] == "8180"
        assert env["KANTA_LAB_API_V2_PATH"] == "/p/kanta-api-v2"

    def test_missing_file_returns_empty(self) -> None:
        assert parse_env_file("/nope/x.env") == {}


def _fake_kanta_docker(tmp: str) -> str:
    """Build a fake kanta-docker tree with an executable kanta-stack stub."""
    bin_dir = os.path.join(tmp, "kanta-docker", "bin")
    lab_dir = os.path.join(tmp, "kanta-docker", "dev", "lab")
    os.makedirs(bin_dir)
    os.makedirs(os.path.join(lab_dir, "instances"))
    open(os.path.join(lab_dir, ".env"), "w").close()  # prerequisite
    bin_path = os.path.join(bin_dir, "kanta-stack")
    with open(bin_path, "w") as fh:
        fh.write("#!/usr/bin/env bash\nexit 0\n")
    os.chmod(bin_path, os.stat(bin_path).st_mode | stat.S_IEXEC)
    return bin_path


def _write_instance(bin_path: str, slug: str, port: int = 8180) -> None:
    instances = os.path.join(os.path.dirname(os.path.dirname(bin_path)), "dev", "lab", "instances")
    task_dir = f"/tasks/{slug}"
    with open(os.path.join(instances, f"{slug}.env"), "w") as fh:
        fh.write(
            f"KANTA_LAB_PORT={port}\n"
            f"KANTA_LAB_API_PATH={task_dir}/kanta-api\n"
            f"KANTA_LAB_API_V2_PATH={task_dir}/kanta-api-v2\n"
            f"KANTA_LAB_FRONT_PATH={task_dir}/kanta-front\n"
            f"KANTA_LAB_FRONT_V2_PATH={task_dir}/kanta-front-v2\n"
        )


class TestKantaStackIntrospection:
    def test_prerequisite_ok(self) -> None:
        tmp = tempfile.mkdtemp()
        ks = KantaStack(_fake_kanta_docker(tmp))
        assert ks.prerequisite_error() is None

    def test_prerequisite_missing_bin(self) -> None:
        ks = KantaStack("/nope/kanta-stack")
        assert "not found" in (ks.prerequisite_error() or "")

    def test_prerequisite_missing_lab_env(self) -> None:
        tmp = tempfile.mkdtemp()
        bin_path = _fake_kanta_docker(tmp)
        os.remove(os.path.join(os.path.dirname(os.path.dirname(bin_path)), "dev", "lab", ".env"))
        assert ".env" in (ks := KantaStack(bin_path)).prerequisite_error()

    def test_workspace_paths_and_port(self) -> None:
        tmp = tempfile.mkdtemp()
        bin_path = _fake_kanta_docker(tmp)
        _write_instance(bin_path, "kan-1", port=8280)
        ks = KantaStack(bin_path)
        paths = ks.workspace_paths("kan-1")
        assert paths["kanta-api-v2"] == "/tasks/kan-1/kanta-api-v2"
        assert paths["kanta-front-v2"] == "/tasks/kan-1/kanta-front-v2"
        assert ks.port("kan-1") == 8280

    def test_instance_exists(self) -> None:
        tmp = tempfile.mkdtemp()
        bin_path = _fake_kanta_docker(tmp)
        ks = KantaStack(bin_path)
        assert ks.instance_exists("ghost") is False
        _write_instance(bin_path, "real")
        assert ks.instance_exists("real") is True


class TestKantaStackCommands:
    @pytest.mark.asyncio
    async def test_up_returns_workspace_paths(self) -> None:
        # Fake bin exits 0; we pre-write the instance env it would have produced.
        tmp = tempfile.mkdtemp()
        bin_path = _fake_kanta_docker(tmp)
        _write_instance(bin_path, "kan-5")
        ks = KantaStack(bin_path)
        paths = await ks.up("kan-5", timeout=10)
        assert paths["kanta-api-v2"] == "/tasks/kan-5/kanta-api-v2"

    @pytest.mark.asyncio
    async def test_up_raises_on_missing_prerequisite(self) -> None:
        ks = KantaStack("/nope/kanta-stack")
        with pytest.raises(RuntimeError, match="unavailable"):
            await ks.up("kan-5", timeout=10)

    @pytest.mark.asyncio
    async def test_up_raises_when_no_instance_env_written(self) -> None:
        # Bin exits 0 but produces no instance env → treated as failure.
        tmp = tempfile.mkdtemp()
        bin_path = _fake_kanta_docker(tmp)
        ks = KantaStack(bin_path)
        with pytest.raises(RuntimeError, match="no instance env"):
            await ks.up("kan-6", timeout=10)

    @pytest.mark.asyncio
    async def test_up_raises_on_nonzero_exit(self) -> None:
        tmp = tempfile.mkdtemp()
        bin_path = _fake_kanta_docker(tmp)
        with open(bin_path, "w") as fh:
            fh.write("#!/usr/bin/env bash\necho boom >&2\nexit 3\n")
        os.chmod(bin_path, os.stat(bin_path).st_mode | stat.S_IEXEC)
        ks = KantaStack(bin_path)
        with pytest.raises(RuntimeError, match="failed"):
            await ks.up("kan-7", timeout=10)

    @pytest.mark.asyncio
    async def test_down_noop_when_no_instance(self) -> None:
        tmp = tempfile.mkdtemp()
        ks = KantaStack(_fake_kanta_docker(tmp))
        assert await ks.down("ghost", timeout=10) is False

    @pytest.mark.asyncio
    async def test_down_succeeds_for_existing_instance(self) -> None:
        tmp = tempfile.mkdtemp()
        bin_path = _fake_kanta_docker(tmp)
        _write_instance(bin_path, "kan-8")
        ks = KantaStack(bin_path)
        assert await ks.down("kan-8", timeout=10) is True


class TestForgeProvisionerIntegration:
    """The forge must map worktree folders back to mycel repo keys.

    mycel_config aliases `api: kanta-api-v2`, so a worktree folder named
    `kanta-api-v2` has to resolve to the repo key `api` for plan-driven cwd
    resolution and git ops to keep working.
    """

    def _forge(self, repo_folders):
        from forge import Forge
        from message_bus import MessageBus
        return Forge(
            name="dev", description="", workflow=["analyze"], spells={}, workspace={},
            bus=MessageBus(bus_dir=tempfile.mkdtemp()), bus_dir=tempfile.mkdtemp(),
            repo_folders=repo_folders, provisioner="kanta_stack",
        )

    def test_folder_maps_to_repo_key(self) -> None:
        forge = self._forge({"api": "kanta-api-v2", "front": "kanta-front-v2"})
        assert forge._folder_to_repo_key("kanta-api-v2") == "api"
        assert forge._folder_to_repo_key("kanta-front-v2") == "front"

    def test_unknown_folder_keeps_its_name(self) -> None:
        forge = self._forge({"api": "kanta-api-v2"})
        assert forge._folder_to_repo_key("kanta-api") == "kanta-api"

    def test_dynamic_workspace_true_maps_to_clone_provisioner(self) -> None:
        from forge import Forge
        from message_bus import MessageBus
        forge = Forge(
            name="d", description="", workflow=[], spells={}, workspace={},
            bus=MessageBus(bus_dir=tempfile.mkdtemp()), bus_dir=tempfile.mkdtemp(),
            dynamic_workspace=True,
        )
        assert forge.provisioner == "clone"
