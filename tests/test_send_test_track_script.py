"""The sender accepts issued credentials without putting the secret on argv."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

pytestmark = pytest.mark.ops
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/send_test_track.sh"


def test_sender_passes_issued_secret_only_over_curl_stdin(tmp_path):
    capture = tmp_path / "capture.jsonl"
    fake_curl = tmp_path / "curl"
    fake_curl.write_text("""#!/usr/bin/env python3
import json, os, sys
with open(os.environ['CAPTURE'], 'a') as out:
    out.write(json.dumps({'args': sys.argv[1:], 'config': sys.stdin.read()}) + '\\n')
print('200', end='')
""")
    fake_curl.chmod(0o755)
    secret = "synthetic_test_secret_only"
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ['PATH'],
               CAPTURE=str(capture), ODOGRAPH_TRACKING_SECRET=secret)
    result = subprocess.run([str(SCRIPT), '--username', 'odograph_test_fixture'],
                            env=env, text=True, capture_output=True, check=True)
    rows = [json.loads(line) for line in capture.read_text().splitlines()]
    assert len(rows) > 20
    assert all(secret not in ' '.join(row['args']) for row in rows)
    assert all(row['config'] == f'user = "odograph_test_fixture:{secret}"\n' for row in rows)
    assert secret not in result.stdout + result.stderr


@pytest.mark.parametrize('args', [[], ['--password', 'synthetic'], ['--username', 'owntracks']])
def test_sender_refuses_implicit_or_legacy_credentials(args):
    env = {key: value for key, value in os.environ.items() if not key.startswith('ODOGRAPH_TRACKING_')}
    result = subprocess.run([str(SCRIPT), *args], env=env, text=True, capture_output=True)
    assert result.returncode != 0
