"""The startup notice of recordings an earlier session did not transcribe."""

from __future__ import annotations

import time
from datetime import datetime

import pytest
from PySide6 import QtWidgets

import stt_app.main as main_module
from stt_app.config import LANGUAGE_MODE_LABELS
from stt_app.last_recording_store import LastRecordingStore
from stt_app.transcript_history import TranscriptHistoryEntry, TranscriptHistoryStore
from stt_app.unfinished_recordings import UnfinishedRecordingStore
from stt_app.unfinished_recordings_dialog import UnfinishedRecordingsDialog


@pytest.fixture
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def stores(tmp_path):
    last = LastRecordingStore(
        audio_path=tmp_path / "last_recording.wav",
        state_path=tmp_path / "last_recording.json",
    )
    history = TranscriptHistoryStore(path=tmp_path / "history.json")
    unfinished = UnfinishedRecordingStore(tmp_path / "unfinished")
    return last, history, unfinished


def _keep(unfinished, recording_id, audio=b"RIFF", stamp="2026-10-08T21:15:00"):
    unfinished.save(
        audio, recording_id=recording_id, recorded_at=datetime.fromisoformat(stamp)
    )
    return unfinished.find(recording_id)


def _history_entry(text, recording_id):
    return TranscriptHistoryEntry.new(
        text=text,
        engine="local",
        model="small",
        mode="import",
        source_recording_id=recording_id,
    )


def _offer(stores, tmp_path, transcribe=None, language_note=None):
    last, history, unfinished = stores
    return main_module._offer_unfinished_recordings(
        last_recording_store=last,
        history_store=history,
        unfinished_store=unfinished,
        transcribe=transcribe or (lambda _recording, _progress: (True, "text")),
        keep_dir=tmp_path / "recordings",
        language_note=language_note,
    )


def _wait_until(app, condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        app.processEvents()
        time.sleep(0.01)


def test_the_notice_lists_the_quits_recordings_and_the_last_recording(
    app, stores, tmp_path
):
    """The managed slot joins the list instead of getting its own prompt,
    and is handed over: marked completed, so it is not offered twice."""
    last, _history, unfinished = stores
    _keep(unfinished, "queued1", audio=b"RIFF-queued")
    state = last.save_recording(b"RIFF-last", keep_after_success=False)
    last.mark_transcribing(engine="local", model="small", mode="batch")

    dialog = _offer(stores, tmp_path)

    assert dialog is not None and dialog.isVisible()
    assert dialog.table.rowCount() == 2
    kept = {item.recording_id: item for item in unfinished.list_recordings()}
    assert kept[state.recording_id].path.read_bytes() == b"RIFF-last"
    assert last.has_recoverable_recording() is False
    assert not last.audio_path.exists(), "keep_after_success off: the copy is the one"
    dialog.close()


def test_the_last_recordings_language_goes_with_it_into_the_list(app, stores, tmp_path):
    """Owner decision 2026-10-17: a retry uses the language the recording was
    made in. The managed slot is the only place that language is stored
    after a crash, so the copy into the list must carry it."""
    last, _history, unfinished = stores
    last.save_recording(b"RIFF-german", keep_after_success=False, language_mode="de")
    last.mark_failed("network")
    _offer(stores, tmp_path).close()
    # A state without a language (an earlier version's) is kept as unknown.
    last.save_recording(b"RIFF-older", keep_after_success=False)
    last.mark_failed("network")
    _offer(stores, tmp_path).close()

    languages = {
        item.path.read_bytes(): item.language_mode
        for item in unfinished.list_recordings()
    }

    assert languages == {b"RIFF-german": "de", b"RIFF-older": ""}


def test_a_slot_that_cannot_be_marked_is_not_copied_twice(
    app, stores, tmp_path, monkeypatch
):
    last, _history, unfinished = stores
    last.save_recording(b"RIFF-last", keep_after_success=False)
    last.mark_failed("network")

    def _refuse(**_kwargs):
        raise OSError("locked")

    monkeypatch.setattr(last, "mark_completed", _refuse)

    _offer(stores, tmp_path).close()
    _offer(stores, tmp_path).close()

    assert len(unfinished.list_recordings()) == 1


def test_a_recording_already_in_history_is_not_offered(app, stores, tmp_path):
    """Its transcript exists: the leftover file is deleted, unless the
    transcript has a gap, whose recording is kept on purpose."""
    _last, history, unfinished = stores
    done = _keep(unfinished, "done1")
    gap = _keep(unfinished, "gap1")
    history.add_entry(_history_entry("all there", "done1"), max_items=20)
    history.add_entry(
        _history_entry("half [no text returned for 0:10-0:20] there", "gap1"),
        max_items=20,
    )

    dialog = _offer(stores, tmp_path)

    assert dialog is None
    assert not done.path.exists()
    assert gap.path.exists()


def test_a_file_a_history_entry_points_at_is_not_deleted(app, stores, tmp_path):
    """A transcribed recording whose move to the recordings folder failed
    stays in the unfinished folder with its entry pointing at it; the next
    start must not delete the audio that entry links to."""
    import os

    _last, history, unfinished = stores
    linked = _keep(unfinished, "linked1")
    entry = TranscriptHistoryEntry.new(
        text="all there",
        engine="local",
        model="small",
        mode="import",
        source_recording_id="linked1",
        source_audio_path=os.path.abspath(linked.path),
    )
    history.add_entry(entry, max_items=20)

    assert _offer(stores, tmp_path) is None
    assert linked.path.exists()


def test_nothing_unfinished_shows_nothing(app, stores, tmp_path):
    assert _offer(stores, tmp_path) is None


def _dialog(
    tmp_path,
    unfinished,
    *,
    transcribe=None,
    reveal=None,
    confirm=None,
    language_note=None,
):
    return UnfinishedRecordingsDialog(
        recordings=unfinished.list_recordings(),
        store=unfinished,
        transcribe=transcribe or (lambda _recording, _progress: (True, "text")),
        keep_dir=tmp_path / "recordings",
        reveal=reveal or (lambda _paths: True),
        confirm_delete=confirm or (lambda _parent, _count: True),
        language_note=language_note,
    )


def test_show_in_folder_selects_the_chosen_files_and_keeps_the_notice_open(
    app, stores, tmp_path
):
    """The owner listens to the files before deciding."""
    _last, _history, unfinished = stores
    first = _keep(unfinished, "a1", stamp="2026-10-08T10:00:00")
    second = _keep(unfinished, "b2", stamp="2026-10-08T11:00:00")
    _keep(unfinished, "c3", stamp="2026-10-08T12:00:00")
    revealed = []
    dialog = _dialog(tmp_path, unfinished, reveal=lambda paths: revealed.append(paths))
    dialog.show()

    dialog.table.selectRow(0)
    dialog.table.setRangeSelected(
        QtWidgets.QTableWidgetSelectionRange(1, 0, 1, 3), True
    )
    dialog.reveal_button.click()

    assert revealed == [[first.path, second.path]]
    assert dialog.isVisible()
    dialog.table.clearSelection()
    dialog.reveal_button.click()
    assert len(revealed[-1]) == 3, "with nothing selected, all are revealed"
    dialog.close()


def test_transcribe_all_reports_each_row_and_keeps_a_failure_actionable(
    app, stores, tmp_path
):
    _last, _history, unfinished = stores
    _keep(unfinished, "ok1", stamp="2026-10-08T10:00:00")
    _keep(unfinished, "bad2", stamp="2026-10-08T11:00:00")
    seen = []

    def _transcribe(recording, _progress):
        seen.append(recording.recording_id)
        if recording.recording_id == "bad2":
            return False, "provider down"
        return True, "hello"

    dialog = _dialog(tmp_path, unfinished, transcribe=_transcribe)
    dialog.show()

    dialog.transcribe_all_button.click()
    _wait_until(app, lambda: dialog.transcribe_all_button.isEnabled())

    assert seen == ["ok1", "bad2"]
    status = [dialog.table.item(row, 3).text() for row in range(2)]
    assert status == ["Transcribed, in History", "Failed: provider down"]
    assert "1 failed" in dialog.status_label.text()
    dialog.table.selectRow(1)
    assert dialog.transcribe_selected_button.isEnabled()
    dialog.table.selectRow(0)
    assert not dialog.transcribe_selected_button.isEnabled()
    dialog.close()


def test_a_row_says_which_language_ran_when_the_recorded_one_was_not_offered(
    app, stores, tmp_path
):
    """The Retry after a restart falls back to the selected language where the
    engine does not offer the recorded one (`transcribe_unfinished_recording`);
    the row says so, since the transcript is in History and the user judges it
    by the language it was made in."""
    _last, _history, unfinished = stores
    _keep(unfinished, "a1", stamp="2026-10-08T10:00:00")
    _keep(unfinished, "b2", stamp="2026-10-08T11:00:00")
    notes = {"a1": ("English", "Auto"), "b2": None}
    dialog = _dialog(
        tmp_path,
        unfinished,
        language_note=lambda recording: notes[recording.recording_id],
    )
    dialog.show()

    dialog.transcribe_all_button.click()
    _wait_until(app, lambda: "into History and listed" in dialog.status_label.text())

    cells = [dialog.table.item(row, 3) for row in range(2)]
    assert [cell.text() for cell in cells] == [
        "Transcribed in English, in History (Auto is not offered by this model)",
        "Transcribed, in History",
    ]
    assert cells[0].toolTip().startswith(cells[0].text())
    assert "recording is kept" in cells[0].toolTip()
    assert cells[1].toolTip() == cells[1].text()
    dialog.close()


def test_the_longest_row_text_fits_the_status_column(app, stores, tmp_path):
    """The row is one line, elided on the right: a language label long enough
    to push "is not offered by this model" out of the column would hide the
    very words that tell why the language is another."""
    _last, _history, unfinished = stores
    _keep(unfinished, "a1")
    longest = max(LANGUAGE_MODE_LABELS.values(), key=len)
    dialog = _dialog(
        tmp_path, unfinished, language_note=lambda _recording: (longest, longest)
    )
    dialog.show()

    dialog.transcribe_all_button.click()
    _wait_until(app, lambda: "into History and listed" in dialog.status_label.text())

    cell = dialog.table.item(0, 3)
    table = dialog.table
    # A cell's text keeps this margin on both sides (Qt's item text layout).
    margin = table.style().pixelMetric(QtWidgets.QStyle.PM_FocusFrameHMargin) + 1
    needed = table.fontMetrics().horizontalAdvance(cell.text()) + 2 * margin
    assert needed <= table.columnWidth(3), (cell.text(), needed)
    dialog.close()


def test_a_failing_language_note_does_not_fail_the_transcription(
    app, stores, tmp_path, caplog
):
    """The note only describes the transcription; when it cannot be worked out
    the recording is still transcribed, and the failure is in the log. Asked
    inside the transcription's own `try`, its exception became the row's
    failure and the audio was never sent."""
    _last, _history, unfinished = stores
    _keep(unfinished, "a1")
    transcribed = []

    def _transcribe(recording, _progress):
        transcribed.append(recording.recording_id)
        return True, "hello"

    def _broken_note(_recording):
        raise RuntimeError("settings unreadable")

    dialog = _dialog(
        tmp_path, unfinished, transcribe=_transcribe, language_note=_broken_note
    )
    dialog.show()

    dialog.transcribe_all_button.click()
    _wait_until(app, lambda: "into History and listed" in dialog.status_label.text())

    assert transcribed == ["a1"]
    assert dialog.table.item(0, 3).text() == "Transcribed, in History"
    assert "settings unreadable" in caplog.text
    dialog.close()


def test_the_startup_offer_hands_the_language_note_to_its_notice(app, stores, tmp_path):
    _last, _history, unfinished = stores
    _keep(unfinished, "a1")
    dialog = _offer(
        stores, tmp_path, language_note=lambda _recording: ("German", "Auto")
    )

    dialog.transcribe_all_button.click()
    _wait_until(app, lambda: "into History and listed" in dialog.status_label.text())

    assert dialog.table.item(0, 3).text() == (
        "Transcribed in German, in History (Auto is not offered by this model)"
    )
    dialog.close()


def test_delete_asks_first_and_deletes_only_the_selection(app, stores, tmp_path):
    _last, _history, unfinished = stores
    first = _keep(unfinished, "a1", stamp="2026-10-08T10:00:00")
    second = _keep(unfinished, "b2", stamp="2026-10-08T11:00:00")
    answers = [False, True]
    asked = []

    def _confirm(_parent, count):
        asked.append(count)
        return answers.pop(0)

    dialog = _dialog(tmp_path, unfinished, confirm=_confirm)
    dialog.table.selectRow(0)

    dialog.delete_button.click()
    assert first.path.exists(), "deleted although the user said no"
    dialog.delete_button.click()

    assert asked == [1, 1]
    assert not first.path.exists()
    assert second.path.exists()
    assert dialog.table.item(0, 3).text() == "Deleted"
    dialog.close()


def test_keep_files_moves_the_rest_to_the_recordings_folder(app, stores, tmp_path):
    _last, _history, unfinished = stores
    _keep(unfinished, "a1")
    dialog = _dialog(tmp_path, unfinished)
    dialog.show()

    dialog.keep_button.click()

    assert not dialog.isVisible()
    assert unfinished.list_recordings() == []
    assert [path.name for path in (tmp_path / "recordings").iterdir()] == [
        "unfinished_20261008_211500_a1.wav"
    ]


def test_a_failed_move_keeps_the_notice_open_and_says_why(
    app, stores, tmp_path, monkeypatch
):
    _last, _history, unfinished = stores
    _keep(unfinished, "a1")
    dialog = _dialog(tmp_path, unfinished)
    dialog.show()

    def _refuse(_recording, _directory):
        raise PermissionError("access denied")

    monkeypatch.setattr(unfinished, "move_to", _refuse)
    dialog.keep_button.click()

    assert dialog.isVisible()
    assert "access denied" in dialog.status_label.text()
    dialog.close()


def test_the_notice_keeps_one_size_while_rows_change(app, stores, tmp_path):
    _last, _history, unfinished = stores
    for index in range(9):
        _keep(unfinished, f"r{index}", stamp=f"2026-10-08T10:0{index}:00")
    dialog = _dialog(
        tmp_path,
        unfinished,
        transcribe=lambda _recording, _progress: (False, "x" * 300),
    )
    dialog.show()
    app.processEvents()
    size = dialog.size()
    keep_geometry = dialog.keep_button.geometry()

    dialog.transcribe_all_button.click()
    _wait_until(app, lambda: dialog.transcribe_all_button.isEnabled())
    dialog.show_problem("A very long problem. " * 6)
    app.processEvents()

    assert dialog.size() == size
    assert dialog.keep_button.geometry() == keep_geometry
    dialog.close()


def test_a_partial_streaming_transcript_does_not_count_as_transcribed(
    app, stores, tmp_path
):
    """A dying stream writes what it heard so far to history under the
    recording's id and keeps the whole audio for Retry. That entry is not the
    transcript: the review of 3ee1e23 measured the kept file deleted unseen."""
    _last, history, unfinished = stores
    kept = _keep(unfinished, "stream1")
    history.add_entry(
        TranscriptHistoryEntry.new(
            text="the first thirty seconds",
            engine="assemblyai",
            model="universal",
            mode="streaming",
            source_recording_id="stream1",
        ),
        max_items=20,
    )

    dialog = _offer(stores, tmp_path)

    assert dialog is not None and dialog.table.rowCount() == 1
    assert kept.path.exists()
    dialog.close()


def test_the_last_recording_of_a_dying_stream_is_offered(app, stores, tmp_path):
    last, history, unfinished = stores
    state = last.save_recording(b"RIFF-whole", keep_after_success=False)
    last.mark_failed("socket closed")
    history.add_entry(
        TranscriptHistoryEntry.new(
            text="the first thirty seconds",
            engine="assemblyai",
            model="universal",
            mode="streaming",
            source_recording_id=state.recording_id,
        ),
        max_items=20,
    )

    dialog = _offer(stores, tmp_path)

    assert dialog is not None
    assert unfinished.find(state.recording_id).path.read_bytes() == b"RIFF-whole"
    dialog.close()


def test_with_nothing_left_the_closing_button_no_longer_promises_to_ask(
    app, stores, tmp_path
):
    _last, _history, unfinished = stores
    _keep(unfinished, "a1")
    dialog = _dialog(tmp_path, unfinished)
    dialog.show()
    app.processEvents()
    geometry = dialog.later_button.geometry()

    dialog.table.selectRow(0)
    dialog.delete_button.click()
    app.processEvents()

    assert dialog.later_button.text() == "Close"
    assert dialog.later_button.geometry() == geometry
    dialog.close()
