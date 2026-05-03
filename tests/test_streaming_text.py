from stt_app.config import STREAMING_REVISION_WORD_WINDOW, STREAMING_STABLE_WORD_GUARD
from stt_app.streaming_text import (
    StreamingTextState,
    best_stream_finalize_tail,
    compute_stream_live_delta,
    normalize_stream_text,
    stream_insertion_text,
)


def test_normalize_stream_text_collapses_whitespace():
    assert normalize_stream_text("  hello\n  world\tagain  ") == "hello world again"


def test_stream_insertion_text_omits_space_before_punctuation():
    assert stream_insertion_text("hello", "world") == " world"
    assert stream_insertion_text("hello", ".") == "."
    assert stream_insertion_text("", "hello") == "hello"


def test_stream_live_delta_keeps_first_partial_revisable():
    delta, committed = compute_stream_live_delta(
        "",
        "",
        "hello world",
        stable_word_guard=STREAMING_STABLE_WORD_GUARD,
        revision_word_window=STREAMING_REVISION_WORD_WINDOW,
    )
    assert delta == "hello world"
    assert committed == ""

    delta, committed = compute_stream_live_delta(
        "",
        "hello world now",
        "hello world now again",
        stable_word_guard=STREAMING_STABLE_WORD_GUARD,
        revision_word_window=STREAMING_REVISION_WORD_WINDOW,
    )
    assert delta == "world now again"
    assert committed == "hello"


def test_stream_live_delta_extends_locked_prefix_consistently():
    delta, committed = compute_stream_live_delta(
        "hello",
        "hello there foo bar",
        "hello there foo bar baz",
        stable_word_guard=STREAMING_STABLE_WORD_GUARD,
        revision_word_window=STREAMING_REVISION_WORD_WINDOW,
    )
    assert delta == "foo bar baz"
    assert committed == "hello there"


def test_stream_finalize_tail_uses_last_partial_when_final_diverges():
    tail = best_stream_finalize_tail(
        "hello world",
        "hello word",
        "hello world plus",
    )

    assert tail == "plus"


def test_streaming_text_state_tracks_revisions_and_finalize_tail():
    state = StreamingTextState(
        stable_word_guard=STREAMING_STABLE_WORD_GUARD,
        revision_word_window=STREAMING_REVISION_WORD_WINDOW,
    )

    first = state.apply_partial("hello world")
    assert first.current_insertion == ""
    assert first.desired_insertion == "hello world"
    assert state.committed_text == ""
    assert state.live_text == "hello world"

    second = state.apply_partial("hello world now")
    assert second.current_insertion == "hello world"
    assert second.desired_insertion == "hello world now"
    assert state.committed_text == ""
    assert state.live_text == "hello world now"

    third = state.apply_partial("hello world now again")
    assert third.current_insertion == " world now"
    assert third.desired_insertion == " world now again"
    assert state.committed_text == "hello"
    assert state.live_text == "hello world now again"

    replacement, final_text = state.finalize("hello world now again")
    assert final_text == "hello world now again"
    assert replacement.current_insertion == " world now again"
    assert replacement.desired_insertion == " world now again"
    assert state.live_text == "hello world now again"
