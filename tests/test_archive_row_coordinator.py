from __future__ import annotations

from tests.test_trips_template import _archive_controller, _render_index


def test_archive_row_writes_use_the_canonical_refresh_coordinator():
    controller = _archive_controller(_render_index())

    before_request = controller.split(
        "document.addEventListener('htmx:beforeRequest'", 1
    )[1].split("document.addEventListener('htmx:beforeSwap'", 1)[0]
    assert "const archiveMutations = new WeakMap();" in controller
    assert "if (!beginWrite())" in before_request
    assert "archiveMutations.set(detail.xhr, true);" in before_request
    assert "archiveWriteSucceeded(event.detail.xhr)" in controller
    assert "finishWrite(true);" in controller


def test_archive_row_validation_response_releases_write_without_refreshing():
    controller = _archive_controller(_render_index())
    settled = controller.split(
        "if (archiveMutations.has(xhr))", 1
    )[1].split("// A generation can hold", 1)[0]

    assert "event.detail.successful && archiveWriteSucceeded(xhr)" in settled
    assert "finishWrite(false);" in settled
    assert "if (!event.detail.successful)" in settled


def test_delete_success_closes_its_dialog_before_refreshing():
    controller = _archive_controller(_render_index())
    mutation = controller.split("if (archiveMutations.has(xhr))", 1)[1]

    assert "endsWith('/delete')" in mutation
    assert "closest?.('dialog')?.close();" in mutation
    assert mutation.index("close();") < mutation.index("finishWrite(true);")


def test_history_miss_during_write_is_retained_and_deferred_until_send():
    controller = _archive_controller(_render_index())
    history = controller.split(
        "document.addEventListener('htmx:historyCacheMiss'", 1
    )[1].split("document.addEventListener('htmx:historyCacheMissLoadError'", 1)[0]

    assert "historyMisses.set(xhr, { token, path, suppress: true });" in history
    assert "scope.retain(token, xhr);" in history
    assert "helpers.defer(() =>" in history
    assert "xhr.abort();" in history
