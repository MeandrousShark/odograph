"""The disposable fixture CLI never puts the setup URL in argv."""
from __future__ import annotations
import os
import secrets
import shutil
import subprocess
from pathlib import Path
import pytest

pytestmark = pytest.mark.ops
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/provision_roles.sh'


def test_shellcheck_reports_no_findings():
    if not shutil.which('shellcheck'):
        pytest.skip('shellcheck is not installed')
    result = subprocess.run(['shellcheck',str(SCRIPT)],capture_output=True,text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_wrapper_passes_only_fixed_mode_in_argv(tmp_path):
    fake = tmp_path / 'python'
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ARG_FILE"\n')
    fake.chmod(0o755)
    password = secrets.token_urlsafe(32)
    url = f'postgresql://owner:{password}@localhost/fixture'
    output = tmp_path / 'args'
    result = subprocess.run(['bash',str(SCRIPT),'--fixture'],env={**os.environ,
        'PYTHON':str(fake),'ARG_FILE':str(output),'DATABASE_URL':url},capture_output=True,text=True)
    assert result.returncode == 0
    assert output.read_text().splitlines() == ['-m','app.role_setup','--fixture']
    assert password not in result.stdout + result.stderr + output.read_text()


@pytest.mark.parametrize('args',[[],['--schema','public'],['--fixture','--account-tables','trips']])
def test_fixture_mode_is_required_and_live_configuration_is_rejected(args):
    result = subprocess.run([str(SCRIPT),*args],env={**os.environ,'DATABASE_URL':''},capture_output=True,text=True)
    assert result.returncode != 0
    assert 'usage:' in result.stderr


def test_missing_database_url_is_rejected():
    env = {k:v for k,v in os.environ.items() if k not in ('DATABASE_URL','PROVISION_DATABASE_URL')}
    result = subprocess.run([str(SCRIPT),'--fixture'],env=env,capture_output=True,text=True)
    assert result.returncode != 0
    assert 'DATABASE_URL is required' in result.stderr


def test_wrapper_uses_python_on_path_without_a_local_virtualenv(tmp_path):
    checkout = tmp_path / "checkout"
    scripts = checkout / "scripts"
    scripts.mkdir(parents=True)
    wrapper = scripts / SCRIPT.name
    shutil.copyfile(SCRIPT, wrapper)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    interpreter = binaries / "python3"
    interpreter.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ARG_FILE"\n')
    interpreter.chmod(0o755)
    output = tmp_path / "args"
    env = {key: value for key, value in os.environ.items() if key != "PYTHON"}
    env.update(PATH=f"{binaries}:{os.environ['PATH']}", ARG_FILE=str(output))
    result = subprocess.run(
        ["bash", str(wrapper), "--fixture"], env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text().splitlines() == ["-m", "app.role_setup", "--fixture"]
