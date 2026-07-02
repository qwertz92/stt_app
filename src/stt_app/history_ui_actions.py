"""Shared transcript-history UI flows for the History dialog and Settings History tab.

Both surfaces expose the same export/import/clear actions against a
``TranscriptHistoryStore`` but differ in how they present success feedback and how
they track/persist the active history limit. This module holds the flow logic exactly
once; each caller only supplies the small bits that differ (feedback presentation,
limit persistence, limit widget updates).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6 import QtWidgets

from .transcript_history import TranscriptHistoryStore


def format_history_count_label(total: int, limit: int) -> str:
    """Render the "Stored: N entries (...)" summary text for a history limit."""
    count = int(total)
    if limit == 0:
        return f"Stored: {count} entries (unlimited; showing all)"

    shown = min(count, limit)
    if count <= limit:
        return f"Stored: {count} entries (limit {limit}; showing all stored entries)"
    return f"Stored: {count} entries (limit {limit}; showing latest {shown})"


def history_import_dialog_dir(history_store: TranscriptHistoryStore) -> str:
    """Default directory for the import file picker: the history store's own folder."""
    path = history_store.path.parent
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return str(Path.home() / "Documents")
    return str(path)


def prompt_import_overflow(
    parent: QtWidgets.QWidget,
    *,
    import_count: int,
    free_slots: int,
    max_items: int,
) -> str:
    """Ask how to handle an import that exceeds the free slots under the active limit.

    Returns ``"free"`` (import only the free slots), ``"unlimited"`` (switch the limit
    to unlimited and import everything), or ``"cancel"``.
    """
    box = QtWidgets.QMessageBox(parent)
    box.setWindowTitle("Import exceeds history size")
    box.setIcon(QtWidgets.QMessageBox.Question)
    box.setText(
        (
            f"Import contains {import_count} entries, but only {free_slots} "
            f"slot{'s' if free_slots != 1 else ''} are free "
            f"(current max: {max_items})."
        )
    )
    box.setInformativeText(
        "Choose whether to import only free slots or switch to unlimited storage."
    )
    free_button = box.addButton(
        f"Import only {free_slots}",
        QtWidgets.QMessageBox.AcceptRole,
    )
    unlimited_button = box.addButton(
        "Import all and set unlimited",
        QtWidgets.QMessageBox.DestructiveRole,
    )
    cancel_button = box.addButton(QtWidgets.QMessageBox.Cancel)
    box.setDefaultButton(free_button)
    box.exec()
    clicked = box.clickedButton()
    if clicked == free_button:
        return "free"
    if clicked == unlimited_button:
        return "unlimited"
    if clicked == cancel_button:
        return "cancel"
    return "cancel"


def run_history_export(
    parent: QtWidgets.QWidget,
    history_store: TranscriptHistoryStore,
    *,
    on_exported: Callable[[int, str], None],
) -> None:
    """Prompt for a destination file and export the full transcript history to it.

    ``on_exported`` receives the exported entry count and the destination path and is
    responsible for presenting success feedback (callers differ here: the dialog pops
    an information box, the Settings tab uses its inline status label). Export
    failures always show a warning box.
    """
    suggested = (
        Path.home()
        / "Documents"
        / f"dictation_history_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    path, _filter = QtWidgets.QFileDialog.getSaveFileName(
        parent,
        "Export transcript history",
        str(suggested),
        "JSON files (*.json);;All files (*)",
    )
    if not path:
        return

    try:
        count = history_store.export_to_file(Path(path))
    except Exception as exc:
        QtWidgets.QMessageBox.warning(
            parent,
            "Export failed",
            f"Failed to export transcript history: {exc}",
        )
        return

    on_exported(count, path)


def run_history_import(
    parent: QtWidgets.QWidget,
    history_store: TranscriptHistoryStore,
    *,
    dialog_dir: str,
    current_limit: int,
    prompt_overflow: Callable[..., str],
    persist_limit: Callable[[int], bool],
    set_limit_widget: Callable[[int], None],
    on_imported: Callable[[int, int], None],
) -> None:
    """Prompt for a file and import transcript history entries from it.

    ``prompt_overflow`` is called with ``import_count``, ``free_slots`` and
    ``max_items`` keyword arguments (see :func:`prompt_import_overflow`) and must
    return ``"free"``, ``"unlimited"`` or ``"cancel"``. ``persist_limit`` persists a
    new history limit (used only when the user switches to unlimited) and returns
    whether the persist succeeded. ``set_limit_widget`` updates the caller's limit
    spin box without re-triggering its own change handler. ``on_imported`` receives
    the number of entries actually appended and the active limit used for the append,
    and is responsible for reloading the list and presenting success feedback.
    """
    path, _filter = QtWidgets.QFileDialog.getOpenFileName(
        parent,
        "Import transcript history",
        dialog_dir,
        "JSON files (*.json);;All files (*)",
    )
    if not path:
        return

    try:
        imported_entries = history_store.import_from_file(Path(path))
    except ValueError as exc:
        QtWidgets.QMessageBox.warning(parent, "Import failed", str(exc))
        return

    if not imported_entries:
        QtWidgets.QMessageBox.information(
            parent,
            "No entries found",
            "The selected file does not contain importable transcript entries.",
        )
        return

    active_limit = current_limit
    current_count = history_store.count()
    entries_to_append = imported_entries

    if active_limit > 0:
        free_slots = max(0, active_limit - current_count)
        if len(imported_entries) > free_slots:
            decision = prompt_overflow(
                import_count=len(imported_entries),
                free_slots=free_slots,
                max_items=active_limit,
            )
            if decision == "cancel":
                return
            if decision == "free":
                if free_slots <= 0:
                    QtWidgets.QMessageBox.information(
                        parent,
                        "No free slots",
                        "History is already full. Increase the limit or use unlimited mode.",
                    )
                    return
                entries_to_append = imported_entries[:free_slots]
            else:
                if not persist_limit(0):
                    return
                active_limit = 0
                set_limit_widget(0)

    imported_count = history_store.append_entries(
        entries_to_append,
        max_items=active_limit,
    )
    on_imported(imported_count, active_limit)


def run_history_clear(
    parent: QtWidgets.QWidget,
    history_store: TranscriptHistoryStore,
    *,
    on_cleared: Callable[[], None],
) -> None:
    """Confirm and clear the entire transcript history."""
    count = history_store.count()
    if count <= 0:
        QtWidgets.QMessageBox.information(
            parent,
            "History is empty",
            "There are no history entries to clear.",
        )
        return
    answer = QtWidgets.QMessageBox.question(
        parent,
        "Clear history",
        (
            f"This will permanently delete {count} "
            f"entr{'y' if count == 1 else 'ies'}.\n\nContinue?"
        ),
        QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        QtWidgets.QMessageBox.No,
    )
    if answer != QtWidgets.QMessageBox.Yes:
        return

    history_store.clear()
    on_cleared()
