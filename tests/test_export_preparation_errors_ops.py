"""Actual isolated XLSX helper preserves cell failures and prefix behavior."""
import asyncio

from openpyxl.utils.exceptions import IllegalCharacterError
import pytest

from app.account_context import AccountPrincipal
from app.capacity import AdmissionManager
from app.export import to_xlsx
from app.export_preparation import ExportProjection
from app.preparation import PreparationOperation
from app.rates import YearRate
from app.report_preparation import _encode
from test_streamed_exports import TZ, signature, trip

pytestmark = pytest.mark.ops


@pytest.mark.parametrize('prefix_length', [32766, 32767], ids=['illegal-in-prefix', 'illegal-after-prefix'])
def test_actual_export_helper_preserves_illegal_cell_category_and_cleanup(tmp_path, prefix_length):
    row = trip(1)
    row['notes'] = '車' * prefix_length + '\x01private cell suffix'
    rates = {2026: YearRate(.7)}
    if prefix_length == 32766:
        with pytest.raises(IllegalCharacterError):
            to_xlsx([row], rates, TZ)
        expected = None
    else:
        expected = to_xlsx([row], rates, TZ)

    async def run():
        manager = AdmissionManager()
        async with manager.operation('foreground', AccountPrincipal(1, True, 1)) as owner:
            async with PreparationOperation(spool_root=tmp_path / 'spool') as operation:
                async with manager.lease((1,)):
                    async def prepare():
                        session = await operation.start_helper(mode='export')
                        projection = ExportProjection(operation, session)
                        await projection.literal_text('display_tz', str(TZ))
                        await projection.timezone_paths()
                        for key in ('category', 'from', 'to', 'vehicle', 'q', 'exclusion'):
                            await projection.literal_text(key, '')
                        await session.request({'type': 'initialize', 'metadata': {
                            'format': 'xlsx', 'client_codec': 'utf8'}})
                        await projection.literal_text('rate1', '.7')
                        await session.send_command({'type': 'rate', 'row': {
                            'year': 2026, 'has_rate2': False, 'h2_start_month': None}})
                        offset = projection.text_offset
                        await projection.literal_text('field:notes', row['notes'])
                        projected = _encode(row)
                        projected['notes'] = {'text': [offset, len(row['notes'].encode('utf8')), 'utf8']}
                        await session.send_command({'type': 'detail', 'row': projected})
                        result = await session.request({'type': 'render'})
                        await operation.finish_preparation()
                        return result

                    if expected is None:
                        with pytest.raises(IllegalCharacterError) as failure:
                            await operation.perform(prepare)
                        assert str(failure.value) == 'preparation cell is invalid'
                        assert 'private cell suffix' not in str(failure.value)
                        assert not operation.finished
                        assert not operation.budget.path('trips.xlsx').exists()
                        await operation.close()
                    else:
                        result = await operation.perform(prepare)
                        assert operation.finished and operation.process.returncode == 0
                        messages = []
                        async def send(message):
                            assert manager.snapshot()['leases'] == 1
                            assert not owner._lifetime.released
                            messages.append(message)
                        response = operation.prepared(result['path'], media_type=result['media_type'],
                                                      filename=result['filename'])
                        await response({}, None, send)
                        actual = b''.join(message.get('body', b'') for message in messages)
                        assert signature(actual) == signature(expected)
                        assert messages[0]['status'] == 200 and messages[-1]['more_body'] is False
                    assert operation.closed and operation.process.returncode is not None
                    assert not operation.directory.exists()
                    assert not owner._lifetime.threads and not owner._lifetime.released
                    assert manager.snapshot()['leases'] == 1
                assert not list((tmp_path / 'spool').glob('op-*'))
            assert manager.snapshot()['leases'] == 0
        assert owner._lifetime.released
        assert manager.snapshot()['foreground'] == {'active': 0, 'pending': 0}
    asyncio.run(run())
