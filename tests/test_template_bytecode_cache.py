"""The test-owned template bytecode cache (see tests/conftest.py).

Production builds templates uncached; tests share compiled code across fresh
environments, and each environment must still render its own config.
"""
from __future__ import annotations

from types import SimpleNamespace
from zoneinfo import ZoneInfo

import jinja2
from fastapi.templating import Jinja2Templates
import pytest

import app.main
from app.main import make_templates
from conftest import MemoryBytecodeCache, template_bytecode_cache

USER = {"id": 41, "name": "Tester", "is_admin": True}


def _config(tz: str, version: str, tiles: str):
    return SimpleNamespace(display_tz=ZoneInfo(tz), app_version=version, map_tile_url=tiles)


def _render(templates) -> tuple[str, str]:
    script = templates.env.get_template("_manual_trip_script.html").render()
    page = templates.env.get_template("base.html").render(user=USER)
    return script, page.split("<footer>", 1)[1]


def test_production_templates_are_built_without_a_bytecode_cache(monkeypatch):
    monkeypatch.setattr(app.main, "Jinja2Templates", Jinja2Templates)

    templates = make_templates(_config("UTC", "uncached-version", "https://uncached.invalid/{z}/{x}/{y}.png"))

    assert templates.env.bytecode_cache is None
    script, footer = _render(templates)
    assert '"UTC"' in script
    assert "uncached.invalid" in script
    assert "uncached-version" in footer


def test_cached_environments_keep_their_own_config(monkeypatch):
    first = make_templates(_config("Europe/Paris", "first-version", "https://first.invalid/{z}/{x}/{y}.png"))
    second = make_templates(_config("Asia/Tokyo", "second-version", "https://second.invalid/{z}/{x}/{y}.png"))
    assert first.env is not second.env
    assert first.env.bytecode_cache is template_bytecode_cache
    assert second.env.bytecode_cache is template_bytecode_cache

    first_script, first_footer = _render(first)
    compiles = []
    original_compile = jinja2.Environment.compile

    def counted_compile(env, *args, **kwargs):
        compiles.append(env)
        return original_compile(env, *args, **kwargs)

    monkeypatch.setattr(jinja2.Environment, "compile", counted_compile)
    second_script, second_footer = _render(second)
    again_script, again_footer = _render(make_templates(
        _config("Europe/Paris", "first-version", "https://first.invalid/{z}/{x}/{y}.png")))

    assert compiles == [], "the second environment reuses compiled code"
    assert '"Europe/Paris"' in first_script and "first.invalid" in first_script
    assert "first-version" in first_footer
    assert '"Asia/Tokyo"' in second_script and "second.invalid" in second_script
    assert "second-version" in second_footer
    assert "Europe/Paris" not in second_script and "first.invalid" not in second_script
    assert "first-version" not in second_footer
    assert (again_script, again_footer) == (first_script, first_footer)


@pytest.mark.parametrize("cached", [False, True])
def test_edited_source_compiles_again(tmp_path, cached):
    (tmp_path / "page.html").write_text("before {{ value }}")
    cache = MemoryBytecodeCache() if cached else None

    def render():
        env = jinja2.Environment(loader=jinja2.FileSystemLoader(tmp_path), bytecode_cache=cache)
        return env.get_template("page.html").render(value=1)

    assert render() == "before 1"
    assert render() == "before 1"
    (tmp_path / "page.html").write_text("after {{ value }}")
    assert render() == "after 1"
