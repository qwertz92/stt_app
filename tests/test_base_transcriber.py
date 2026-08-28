"""Tests for transcriber base interface defaults."""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

import stt_app.transcriber
from stt_app.transcriber.base import (
    ITranscriber,
    TranscriptionCanceled,
    strip_language_tags,
)


class MinimalTranscriber(ITranscriber):
    """Concrete subclass that only implements transcribe_batch."""

    def transcribe_batch(self, wav_bytes: bytes) -> str:
        return "text"


def test_default_start_stream_raises():
    t = MinimalTranscriber()
    with pytest.raises(NotImplementedError):
        t.start_stream()


def test_default_push_audio_chunk_raises():
    t = MinimalTranscriber()
    with pytest.raises(NotImplementedError):
        t.push_audio_chunk(b"chunk")


def test_default_stop_stream_raises():
    t = MinimalTranscriber()
    with pytest.raises(NotImplementedError):
        t.stop_stream()


def test_default_abort_stream_raises():
    t = MinimalTranscriber()
    with pytest.raises(NotImplementedError):
        t.abort_stream()


def test_no_cancel_check_never_cancels():
    t = MinimalTranscriber()
    assert t._is_cancel_requested() is False
    t._raise_if_canceled()


def test_a_requested_cancel_raises():
    t = MinimalTranscriber()
    t.set_cancel_check(lambda: True)
    assert t._is_cancel_requested() is True
    with pytest.raises(TranscriptionCanceled):
        t._raise_if_canceled()


def test_a_raising_cancel_check_is_logged_once_not_once_per_poll(caplog):
    """A broken check must not fail the run, and must not flood the log.

    The ONNX/WebGPU reader polls this every 0.25 s for the whole
    transcription, so logging the traceback per poll wrote the same stack
    several times a second and buried everything else in the log file.
    """
    t = MinimalTranscriber()
    t.set_cancel_check(lambda: (_ for _ in ()).throw(ValueError("check exploded")))

    with caplog.at_level(logging.ERROR, logger="stt_app.transcriber.base"):
        for _ in range(5):
            assert t._is_cancel_requested() is False
            t._raise_if_canceled()

    tracebacks = [record for record in caplog.records if record.exc_info]
    assert len(tracebacks) == 1
    assert "MinimalTranscriber" in tracebacks[0].getMessage()


# Classes that own a field of this name without being a transcriber. Every
# entry is a place the scan below deliberately does not protect, so keep it
# short and say why.
_CANCEL_CHECK_FIELD_OWNERS = {
    # A helper thread that owns the very check it polls; it has no base
    # setter to go through.
    "_CancelWatchdog",
}


def _classes_assigning_the_cancel_check(path: Path) -> list[str]:
    """Every `self._cancel_check = ...` in one file, with its class, by AST.

    Deliberately *not* restricted to classes whose bases name `ITranscriber`.
    That filter looked precise and was porous in four ways, each of which a
    new runtime could hit: a base imported under an alias, a subclass of a
    subclass, an annotated assignment, and `setattr(self, "_cancel_check",
    ...)`. Flagging every assignment and naming the one legitimate owner is
    both simpler and strictly more conservative. A plain text scan is still
    not enough -- it cannot tell which class the `self` belongs to.

    Covered: plain assignment, annotated assignment, tuple/list unpacking at
    any nesting depth, `setattr(self, ...)`, and the `__setattr__` forms --
    `object.__setattr__(self, ...)` and `super().__setattr__(...)` -- which are
    the idiomatic way to set a field on a frozen dataclass or a class with a
    custom `__setattr__`, and so the likeliest of the exotic shapes to appear
    for real.

    Still invisible and accepted: `self.__dict__[...]`, `vars(self)[...]`, a
    `for`/`with` target, an aliased `self`, a computed attribute name, and an
    assignment made through a *helper* rather than through `self`
    (`def _install(t, cb): t._cancel_check = cb`). None is idiomatic here;
    matching a subscript assignment on an arbitrary expression would start
    flagging unrelated dictionaries, and matching `<anything>._cancel_check`
    would flag every legitimate caller, including
    `controller._set_transcriber_cancel_check` one layer up.

    Deliberately **not** narrowed: an assignment inside `__init__` is reported
    too. It is usually a harmless `self._cancel_check = None` re-initialiser,
    but the shape that must be caught -- `def __init__(self, cancel_check):
    self._cancel_check = cancel_check` -- is indistinguishable from it without
    tracking where the value came from. A rare false positive on a first-party
    lint is the cheaper error, so the message below names the way out.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[str] = []

    def _is_self_cancel_check(target: ast.expr) -> bool:
        return (
            isinstance(target, ast.Attribute)
            and target.attr == "_cancel_check"
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
        )

    def _names_the_field(node: ast.expr) -> bool:
        return isinstance(node, ast.Constant) and node.value == "_cancel_check"

    def _is_setattr_call(node: ast.AST) -> bool:
        """`setattr(self, ...)`, `object.__setattr__(self, ...)`, `super().__setattr__(...)`.

        The `__setattr__` pair is the idiomatic way past a frozen dataclass or
        a custom `__setattr__`, and a scan that required `node.func` to be a
        bare `ast.Name` saw neither of them.
        """
        if not isinstance(node, ast.Call):
            return False
        if isinstance(node.func, ast.Name) and node.func.id == "setattr":
            return (
                len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "self"
                and _names_the_field(node.args[1])
            )
        if isinstance(node.func, ast.Attribute) and node.func.attr == "__setattr__":
            # `object.__setattr__(self, "_cancel_check", ...)` passes self
            # explicitly; `super().__setattr__("_cancel_check", ...)` does not.
            if (
                len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "self"
                and _names_the_field(node.args[1])
            ):
                return True
            return bool(node.args) and _names_the_field(node.args[0])
        return False

    def _flatten(target: ast.expr) -> list[ast.expr]:
        """Unpacking nests: `(self._cancel_check, x), y = ...` is two levels."""
        if isinstance(target, ast.Tuple | ast.List):
            flat: list[ast.expr] = []
            for element in target.elts:
                flat.extend(_flatten(element))
            return flat
        return [target]

    def _visit(node: ast.AST, class_name: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                # The *nearest* enclosing class owns the assignment, so a
                # nested class is not blamed on the one around it.
                _visit(child, child.name)
                continue
            targets: list[ast.expr] = []
            if isinstance(child, ast.Assign):
                for target in child.targets:
                    # `self._cancel_check, self._other = a, b` -- a subclass
                    # setting two fields on one line walked straight past a
                    # scan that only looked at the target itself. Recursive,
                    # because unpacking nests.
                    targets.extend(_flatten(target))
            elif isinstance(child, ast.AnnAssign):
                targets = [child.target]
            hit = any(_is_self_cancel_check(target) for target in targets)
            hit = hit or _is_setattr_call(child)
            if hit and class_name not in _CANCEL_CHECK_FIELD_OWNERS:
                offenders.append(
                    f"{path.name}:{child.lineno}: "
                    f"{class_name or '<module>'} assigns self._cancel_check "
                    "directly -- route it through "
                    "`super().set_cancel_check(...)` so the latch is reset, "
                    "or add the class to _CANCEL_CHECK_FIELD_OWNERS if it "
                    "genuinely owns the field (an __init__ initialiser is "
                    "reported here too; see the scan's docstring)"
                )
            _visit(child, class_name)

    _visit(tree, None)
    return offenders


def test_no_transcriber_assigns_the_cancel_check_behind_the_base_setter():
    """A source scan, because the instance test cannot see a *future* subclass.

    `set_cancel_check` is overridden in exactly one place today, so the
    instance loop below asserts a property two of its three subjects get for
    free. What it cannot catch is the next runtime reintroducing
    `self._cancel_check = cancel_check`, which is the mistake that was
    actually made: it skips the latch reset, and because a runtime is cached
    for the app's lifetime that turns "logged once per installed check" into
    once per process.
    """
    package = Path(stt_app.transcriber.__file__).parent
    # `rglob`, not `glob`: a runtime added in a subpackage is exactly the kind
    # of new code this guards.
    scanned = [path for path in sorted(package.rglob("*.py")) if path.name != "base.py"]
    assert len(scanned) >= 10, f"the scan found almost nothing: {scanned}"
    offenders: list[str] = []
    for path in scanned:
        offenders.extend(_classes_assigning_the_cancel_check(path))

    assert not offenders, (
        "assign the cancel check through `super().set_cancel_check(...)` so "
        "the once-per-check failure log is re-armed: " + "; ".join(offenders)
    )


def test_every_transcriber_re_arms_the_log_through_the_base_setter():
    """A subclass override must not drop the latch reset.

    The runtimes are cached for the whole app lifetime and a fresh cancel
    check is installed per job, so an override that only assigns
    `_cancel_check` turns "logged once per installed check" into once per
    process -- and the second broken check that session is then silent.
    """
    from stt_app.transcriber.local_faster_whisper import (
        LocalFasterWhisperTranscriber,
    )
    from stt_app.transcriber.local_onnx_asr import LocalOnnxAsrTranscriber

    for transcriber in (
        MinimalTranscriber(),
        LocalFasterWhisperTranscriber(model_size="small"),
        LocalOnnxAsrTranscriber(model_size="parakeet-tdt-0.6b-v3"),
    ):
        transcriber.set_cancel_check(
            lambda: (_ for _ in ()).throw(ValueError("boom"))
        )
        transcriber._is_cancel_requested()
        assert transcriber._cancel_check_failed is True, type(transcriber).__name__

        transcriber.set_cancel_check(lambda: False)

        assert transcriber._cancel_check_failed is False, type(transcriber).__name__


def test_installing_a_new_cancel_check_re_arms_the_log():
    """The latch is per installed check, not per transcriber.

    The runtime is cached and reused across dictations, so latching for the
    object's lifetime would hide a check that starts failing later.
    """
    t = MinimalTranscriber()
    t.set_cancel_check(lambda: (_ for _ in ()).throw(ValueError("boom")))
    t._is_cancel_requested()
    assert t._cancel_check_failed is True

    t.set_cancel_check(lambda: False)

    assert t._cancel_check_failed is False

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("<de-DE> Hallo Welt", " Hallo Welt"),
        ("Hallo <de-DE> Welt", "Hallo Welt"),
        ("<|en|>Hello there", "Hello there"),
        ("<|de-DE|>Hallo", "Hallo"),
        ("<zh-Hans-CN> Ni hao", " Ni hao"),
        ("<es-419> Hola", " Hola"),
        # An upper-case language subtag is still a locale.
        ("<DE-DE> Hallo", " Hallo"),
        ("Text <en-US> mitten drin", "Text mitten drin"),
    ],
)
def test_inline_locale_markers_are_removed_from_a_transcript(text, expected):
    """Nemotron emits its detected locale inline in automatic mode.

    The marker is model metadata that leaked through the decoder, never
    something the user said -- but it was pasted into the document as if it
    were. Reported from real use as "<de-DE>" appearing mid-sentence.
    """
    assert strip_language_tags(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        # Web components and framework tags: hyphenated, and a pattern of
        # "<xx-anything>" deletes every one of them without trace.
        "Web component <my-widget> einbinden",
        "Vue: <el-button> klicken",
        "Ionic <ion-button> ist ein Element",
        "Polymer <dom-if> Template",
        "Angular <ng-container> bleibt",
        "Datei <log-2026> oeffnen",
        # Locale SHAPE but not a language the app knows. Matching by
        # shape alone deleted every one of these from real dictation.
        "Erstelle einen <to-DO> Eintrag",
        "Wirf einen <err-404> Fehler",
        "Lies die <job-ID> aus",
        "Klicke <btn-OK> an",
        "Oeffne <log-100> bitte",
        "Das <|pad|> Token bleibt",
        "<|bos|> und <|eos|> bleiben",
        # 23 of the supported language codes are ordinary English or
        # German words. Matching the region case-insensitively deleted
        # every one of these out of real dictation.
        "Der Code ist <as-is> zu uebernehmen",
        "Das ist ein <no-go> fuer uns",
        "Pruefe ob <is-ok> gesetzt ist",
        "Setze <my-id> auf null",
        "Es war <so-so>",
        "<it-is> wahr und <he-is> da",
        "<or-so> etwa",
        # A lower-case region is deliberately NOT a locale. That is
        # exactly what keeps the words above intact, and no model in
        # use emits "<de-de>" -- the observed forms are "<de-DE>" and
        # "<|en|>".
        "<de-de> bleibt stehen",
        "<en-us> bleibt auch",
        # Plain markup and maths.
        "Verwende <div> und </div>",
        "<tr> ist eine Tabellenzeile, nicht Tuerkisch",
        "<br> und <td> und <html> bleiben",
        "Er sagte a < b und dann c > d",
        "Ich bin <3 dieses Feature",
        "Text ohne Tags",
        "",
    ],
)
def test_stripping_locale_markers_leaves_real_dictation_alone(text):
    """Deleting real speech is a worse bug than the one being fixed.

    A dictation about front-end code is full of hyphenated angle-bracket
    words. Matching subtags by shape -- an uppercase region or a title-case
    script -- is what separates a locale from a component name.
    """
    assert strip_language_tags(text) == text


def test_stripping_preserves_the_spaces_between_decoded_chunks():
    """The streaming path strips per chunk and concatenates the results.

    Trimming the ends of each chunk welds the last word of one onto the
    first word of the next: "Guten" + " Tag" + " heute" became
    "Guten Tagheute".
    """
    chunks = ["Guten", " Tag", " <de-DE> heute", " ist"]
    assert "".join(strip_language_tags(chunk) for chunk in chunks) == (
        "Guten Tag heute ist"
    )


_ALIASED_BASE = """
from .base import ITranscriber as Base


class Aliased(Base):
    def set_cancel_check(self, cancel_check):
        self._cancel_check = cancel_check
"""

_INDIRECT_SUBCLASS = """
from .base import ITranscriber


class Middle(ITranscriber):
    pass


class Leaf(Middle):
    def set_cancel_check(self, cancel_check):
        self._cancel_check = cancel_check
"""

_ANNOTATED_ASSIGNMENT = """
from collections.abc import Callable

from .base import ITranscriber


class Annotated(ITranscriber):
    def set_cancel_check(self, cancel_check):
        self._cancel_check: Callable[[], bool] | None = cancel_check
"""

_SETATTR = """
from .base import ITranscriber


class ViaSetattr(ITranscriber):
    def set_cancel_check(self, cancel_check):
        setattr(self, "_cancel_check", cancel_check)
"""

_NESTED_CLASS = """
from .base import ITranscriber


class Outer(ITranscriber):
    class Inner:
        def arm(self, cancel_check):
            self._cancel_check = cancel_check
"""

_THROUGH_THE_BASE_SETTER = """
from .base import ITranscriber


class Correct(ITranscriber):
    def set_cancel_check(self, cancel_check):
        super().set_cancel_check(cancel_check)
        self._extra = cancel_check
"""

_ALLOWED_OWNER = """
class _CancelWatchdog:
    def __init__(self, cancel_check):
        self._cancel_check = cancel_check
"""


_NESTED_UNPACKING = """
from .base import ITranscriber


class NestedUnpacking(ITranscriber):
    def set_cancel_check(self, cancel_check):
        (self._cancel_check, self._other), self._third = (cancel_check, 1), 2
"""


_OBJECT_SETATTR = """
from .base import ITranscriber


class Frozenish(ITranscriber):
    def set_cancel_check(self, cancel_check):
        object.__setattr__(self, "_cancel_check", cancel_check)
"""


_SUPER_SETATTR = """
from .base import ITranscriber


class CustomSetattr(ITranscriber):
    def set_cancel_check(self, cancel_check):
        super().__setattr__("_cancel_check", cancel_check)
"""


@pytest.mark.parametrize(
    ("label", "source", "expected"),
    [
        ("base imported under an alias", _ALIASED_BASE, ["Aliased"]),
        ("a subclass of a subclass", _INDIRECT_SUBCLASS, ["Leaf"]),
        ("an annotated assignment", _ANNOTATED_ASSIGNMENT, ["Annotated"]),
        ("setattr instead of an assignment", _SETATTR, ["ViaSetattr"]),
        # Unpacking nests, so flattening one level was not enough.
        ("nested tuple unpacking", _NESTED_UNPACKING, ["NestedUnpacking"]),
        # The idiomatic way past a frozen dataclass or a custom __setattr__,
        # and the likeliest of the exotic shapes to turn up for real. Both
        # have an `ast.Attribute` func, which the old scan required to be an
        # `ast.Name`.
        ("object.__setattr__", _OBJECT_SETATTR, ["Frozenish"]),
        ("super().__setattr__", _SUPER_SETATTR, ["CustomSetattr"]),
        # The nearest enclosing class owns it, so the report names `Inner`.
        ("a nested class", _NESTED_CLASS, ["Inner"]),
        ("the correct override", _THROUGH_THE_BASE_SETTER, []),
        ("the one allowed field owner", _ALLOWED_OWNER, []),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_the_cancel_check_scan_sees_each_shape_it_used_to_miss(
    tmp_path, label, source, expected
):
    """Each case here is a way the previous base-name filter was porous."""
    path = tmp_path / "runtime.py"
    path.write_text(source, encoding="utf-8")

    offenders = _classes_assigning_the_cancel_check(path)

    named = [offender.split(": ", 1)[1].split(" assigns")[0] for offender in offenders]
    assert named == expected, f"{label}: {offenders}"


_TUPLE_UNPACKING = """
from .base import ITranscriber


class Unpacked(ITranscriber):
    def set_cancel_check(self, cancel_check):
        self._cancel_check, self._armed = cancel_check, True
"""


def test_the_cancel_check_scan_sees_a_tuple_unpacking_assignment(tmp_path):
    """A subclass setting two fields on one line is the plausible miss."""
    path = tmp_path / "runtime.py"
    path.write_text(_TUPLE_UNPACKING, encoding="utf-8")

    offenders = _classes_assigning_the_cancel_check(path)

    assert [o.split(": ", 1)[1].split(" assigns")[0] for o in offenders] == [
        "Unpacked"
    ], offenders
