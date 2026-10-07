"""Real admitted ASGI report requests for database-backed regression tests."""
from __future__ import annotations

from base64 import b64encode
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from itsdangerous import TimestampSigner
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response

from app.capacity import AdmissionManager
from app.main import make_templates
from app.ui import make_router


async def report_response(pool, path, config):
    # These legacy fixtures selected their timezone through request config.
    # Prepared reports correctly read it from the account snapshot instead.
    async with pool.connection() as conn:
        await conn.execute(
            'UPDATE account_settings SET display_tz=%s WHERE account_id=%s',
            (str(config.display_tz), pool.principal.account_id),
        )
    with TemporaryDirectory(prefix='report-regression-') as directory:
        cfg = SimpleNamespace(**vars(config))
        cfg.dev_no_auth = True
        cfg.app_version = getattr(cfg, 'app_version', 'test')
        cfg.preparation_spool_dir = str(Path(directory) / 'spool')
        app = FastAPI()
        session_secret = 'report-regression-session'
        app.add_middleware(SessionMiddleware, secret_key=session_secret)
        manager = app.state.capacity = AdmissionManager(cfg)
        app.state.config = cfg
        app.state.templates = make_templates(cfg)
        app.state.dev_principal = pool.principal
        app.state.control_pool = manager.manage_pool(pool.control_pool, 'control')
        app.state.runtime_pool = manager.manage_pool(pool.runtime_pool, 'runtime')
        app.include_router(make_router())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url='http://test',
            cookies={'session': TimestampSigner(session_secret).sign(
                b64encode(b'{"csrf":"token"}')).decode()},
        ) as client:
            received = await client.get(path)
            await received.aread()
        snapshot = manager.snapshot()
        assert snapshot['leases'] == 0
        assert all(value['active'] == value['pending'] == 0
                   for key, value in snapshot.items() if key != 'leases')
        assert not list(Path(cfg.preparation_spool_dir).glob('op-*'))
        return Response(
            received.content, status_code=received.status_code,
            headers=dict(received.headers),
            media_type=received.headers.get('content-type', '').split(';', 1)[0],
        )


def freeze_report_time(monkeypatch, tmp_path, instant):
    """Freeze the fresh renderer process while running the real helper main."""
    from app import preparation

    helper = tmp_path / 'frozen-report-helper.py'
    source_root = Path(preparation.__file__).resolve().parent.parent
    helper.write_text(
        'import sys, resource\n'
        'if sys.platform == "linux":\n'
        '    hard = resource.getrlimit(resource.RLIMIT_AS)[1]\n'
        '    ceiling = 256 * 1024 * 1024\n'
        '    if hard != resource.RLIM_INFINITY: ceiling = min(ceiling, hard)\n'
        '    resource.setrlimit(resource.RLIMIT_AS, (ceiling, ceiling))\n'
        f'sys.path.insert(0, {str(source_root)!r})\n'
        'from datetime import datetime\n'
        'from app import preparation_helper, report_renderer\n'
        'class FrozenDatetime(datetime):\n'
        f'    instant = datetime.fromisoformat({instant.isoformat()!r})\n'
        '    @classmethod\n'
        '    def now(cls, tz=None):\n'
        '        return cls.instant if tz is None else cls.instant.astimezone(tz)\n'
        'report_renderer.datetime = FrozenDatetime\n'
        'raise SystemExit(preparation_helper.main())\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(preparation, '_HELPER', helper)
