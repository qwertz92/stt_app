from stt_app.config import STREAMING_REVISION_WORD_WINDOW, STREAMING_STABLE_WORD_GUARD
from stt_app.streaming_text import (
    StreamingTextState,
    append_only_stream_extension_tail,
    append_only_stream_finalize_tail,
    append_only_stream_partial_candidate,
    merge_rolling_window_transcript,
    normalize_stream_text,
    stream_insertion_text,
)


def test_normalize_stream_text_collapses_whitespace():
    assert normalize_stream_text("  hello\n  world\tagain  ") == "hello world again"


def test_stream_insertion_text_omits_space_before_punctuation():
    assert stream_insertion_text("hello", "world") == " world"
    assert stream_insertion_text("hello", ".") == "."
    assert stream_insertion_text("", "hello") == "hello"


def test_append_only_extension_never_rewrites_committed_prefix():
    assert append_only_stream_extension_tail("hello", "hello world") == "world"
    assert append_only_stream_extension_tail("hello world", "hello there") == ""
    assert append_only_stream_extension_tail("hello world", "world again") == ""


def test_append_only_partial_candidate_handles_rolling_audio_window():
    assert (
        append_only_stream_partial_candidate(
            "hello world this is",
            "world this is working",
        )
        == "hello world this is working"
    )


def test_append_only_partial_candidate_keeps_revisions_revisable():
    assert (
        append_only_stream_partial_candidate(
            "hello word",
            "hello world this",
        )
        == "hello world this"
    )
    assert append_only_stream_partial_candidate("hello world", "world again") == (
        "world again"
    )


def test_rolling_window_merge_keeps_text_when_a_window_decodes_to_nothing():
    """An empty trailing window is silence, not a correction. Replacing the
    accumulated text with it produced an empty final transcript for a whole
    dictation whenever the last window decoded to nothing."""
    assert (
        merge_rolling_window_transcript("hello world this is a long dictation", "")
        == "hello world this is a long dictation"
    )
    assert merge_rolling_window_transcript("", "starting up") == "starting up"


def test_rolling_window_merge_reanchors_past_a_wrong_boundary_word():
    """The 8 s window boundary cuts a word in half and every candidate alignment
    is anchored at the window's first word, so one mistranscribed fragment used
    to defeat the search and discard everything spoken so far."""
    assert (
        merge_rolling_window_transcript(
            "alpha bravo charlie delta echo foxtrot",
            "mmh delta echo foxtrot golf hotel",
        )
        == "alpha bravo charlie delta echo foxtrot golf hotel"
    )


def test_rolling_window_merge_replaces_rather_than_appends_when_unalignable():
    """Appending an unalignable window grows without bound while the microphone
    records silence, because the model emits a fresh hallucination on every
    partial and none of them can ever align. Finalization then pastes hundreds
    of junk words into the user's document."""
    accumulated = "so this is the real dictation i spoke"
    for hallucination in (
        "Untertitelung des ZDF",
        "Vielen Dank.",
        "Danke.",
        "Untertitel im Auftrag des ZDF",
    ):
        accumulated = merge_rolling_window_transcript(accumulated, hallucination)

    assert len(accumulated.split()) <= len("so this is the real dictation i spoke".split()) + 5


def test_rolling_window_merge_still_stitches_a_normal_overlap():
    assert (
        merge_rolling_window_transcript("hello world this is", "world this is working")
        == "hello world this is working"
    )


def test_append_only_finalize_uses_only_safe_extensions():
    assert (
        append_only_stream_finalize_tail(
            "hello",
            "hello final",
            "hello partial",
        )
        == "final"
    )
    assert append_only_stream_finalize_tail("hello world", "hello there", "") == ""
    assert (
        append_only_stream_finalize_tail(
            "hello world",
            "hello world",
            "hello world stale",
        )
        == ""
    )
    assert append_only_stream_finalize_tail("hello", "", "hello fallback") == "fallback"


def test_streaming_text_state_append_only_inserts_stable_prefix_only():
    state = StreamingTextState(
        stable_word_guard=STREAMING_STABLE_WORD_GUARD,
        revision_word_window=STREAMING_REVISION_WORD_WINDOW,
    )

    first = state.apply_partial_append_only("hello world")
    assert first.insertion == ""
    assert state.committed_text == ""

    second = state.apply_partial_append_only("hello world this is")
    assert second.insertion == ""
    assert state.committed_text == ""

    third = state.apply_partial_append_only("hello world this is final")
    assert third.insertion == "hello world"
    assert state.committed_text == "hello world"

    revision = state.apply_partial_append_only("hello there this is")
    assert revision.insertion == ""
    assert state.committed_text == "hello world"

    final_insertion, final_text = state.finalize_append_only(
        "hello world this is final"
    )
    assert final_text == "hello world this is final"
    assert final_insertion == " this is final"


def test_streaming_text_state_accumulates_rolling_partials_append_only():
    state = StreamingTextState(
        stable_word_guard=STREAMING_STABLE_WORD_GUARD,
        revision_word_window=STREAMING_REVISION_WORD_WINDOW,
    )

    first = state.apply_partial_append_only("hello world this is")
    assert first.insertion == ""
    assert state.live_text == "hello world this is"

    second = state.apply_partial_append_only("world this is working now")
    assert second.insertion == "hello world"
    assert state.committed_text == "hello world"
    assert state.live_text == "hello world this is working now"

    third = state.apply_partial_append_only("this is working now today")
    assert third.insertion == " this is"
    assert state.committed_text == "hello world this is"
    assert state.live_text == "hello world this is working now today"
