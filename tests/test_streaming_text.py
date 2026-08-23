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


def test_new_segment_window_is_appended_not_aligned():
    """Speech after a long pause must extend the transcript, not replace it.

    A rolling window that follows a silence longer than the window itself shares
    no audio with what is already transcribed, so the overlap search can never
    find a seam. Without the ``new_segment`` marker the unalignable window fell
    through to the replace fallback and everything said before the pause was
    lost.
    """
    merged = merge_rolling_window_transcript(
        "the first thing i said before the pause",
        "and now something completely different",
        new_segment=True,
    )

    assert merged == (
        "the first thing i said before the pause "
        "and now something completely different"
    )


def test_the_append_happens_only_for_a_marked_new_segment():
    """The append must be driven by measured silence, never guessed at.

    Asserting only the default path proves nothing -- it is unchanged, so it
    passes with the whole feature removed. Compare the two calls instead: the
    marker, and nothing else, is what turns a replace into an append. An
    unconditional append grows without bound while the microphone is silent
    and the model hallucinates a fresh window every partial.
    """
    previous = "the first thing i said before the pause"
    window = "and now something completely different"

    replaced = merge_rolling_window_transcript(previous, window)
    appended = merge_rolling_window_transcript(previous, window, new_segment=True)

    assert replaced == window, "an unmarked unalignable window must replace"
    assert appended == f"{previous} {window}"
    assert appended != replaced, (
        "new_segment made no difference; the append path is not wired"
    )


def test_rollback_commit_lets_failed_insertion_text_be_offered_again():
    state = StreamingTextState(stable_word_guard=0, revision_word_window=0)
    state.apply_partial_append_only("hello world")
    before_failed_paste = state.committed_text
    failed = state.apply_partial_append_only("hello world and more")
    assert failed.insertion.strip() == "hello world"

    state.rollback_commit(before_failed_paste)
    assert state.committed_text == before_failed_paste

    # Those words never reached the document, so the next partial has to offer
    # them again. Without the rollback the locked prefix has already moved past
    # them and they can never be inserted for the rest of the session.
    retry = state.apply_partial_append_only("hello world and more still")
    assert retry.insertion.strip().startswith("hello world")


def test_an_unalignable_window_cannot_destroy_an_earlier_segment():
    """One bad window must cost one segment, not the whole dictation.

    This is a reachable chain, not a theoretical one. The post-pause gate is an
    energy measurement and cannot separate a resonant desk thump from a short
    word -- measured, both land around 0.20 s. An admitted thump appends an
    invented sentence; that sentence becomes the text the next real window has
    to align against; the alignment fails; and the replace fallback then wiped
    everything said before the pause.
    """
    spoken_before = "das ist eine lange diktierte passage mit vielen woertern"
    hallucination = "Untertitel von Stephanie Geiges"

    after_pause = merge_rolling_window_transcript(
        spoken_before, hallucination, new_segment=True
    )
    assert after_pause.startswith(spoken_before)

    unprotected = merge_rolling_window_transcript(after_pause, "und weiter geht es")
    assert unprotected == "und weiter geht es", (
        "precondition: an unalignable window replaces everything by default"
    )

    protected = merge_rolling_window_transcript(
        after_pause, "und weiter geht es", protected_prefix=spoken_before
    )
    assert protected == f"{spoken_before} und weiter geht es", (
        f"the earlier segment was destroyed: {protected!r}"
    )


def test_the_protected_prefix_is_ignored_when_it_no_longer_matches():
    """A floor that is not actually a prefix must not be spliced in.

    Otherwise a stale floor from an earlier session or a revised transcript
    would inject text that the provider never produced.
    """
    merged = merge_rolling_window_transcript(
        "voellig anderer text",
        "das neue fenster",
        protected_prefix="etwas das nicht passt",
    )
    assert merged == "das neue fenster"


def test_the_protected_prefix_never_duplicates_text():
    """A window that already contains the floor must not have it prepended.

    Some providers re-emit from the start of the turn, so the window and the
    floor overlap. Joining them then pastes the same words twice, which the
    project treats as worse than losing them.
    """
    # `previous` has to be longer than the floor, or the merge resolves on
    # the ordinary alignment path and never reaches the protected branch --
    # a shorter fixture made an earlier version of this test prove nothing.
    floor = "alpha beta"
    previous = "alpha beta gamma delta"

    assert (
        merge_rolling_window_transcript(previous, floor, protected_prefix=floor)
        == floor
    ), "the floor was prepended to a window that already contained it"
    assert (
        merge_rolling_window_transcript(
            previous, f"{floor} zeta", protected_prefix=floor
        )
        == f"{floor} zeta"
    )
    # And a window that does NOT contain the floor still gets it back.
    assert (
        merge_rolling_window_transcript(
            previous, "zeta eta", protected_prefix=floor
        )
        == f"{floor} zeta eta"
    )


def test_the_protected_prefix_survives_a_window_that_starts_with_punctuation():
    """The case that actually occurs, and the one a word-only check loses.

    `stream_join_text` welds leading punctuation onto the previous word, so
    the floor's last word gains a "." the moment the next window is merged --
    on the very call that pins the floor. A word-based comparison then fails,
    the floor is discarded, and the next unalignable window replaces the
    entire dictation: silently, with nothing in history.
    """
    floor = "Das war der erste Teil"
    after_pause = merge_rolling_window_transcript(
        floor, ". Und jetzt der zweite Teil", new_segment=True
    )
    assert after_pause == "Das war der erste Teil. Und jetzt der zweite Teil"

    survived = merge_rolling_window_transcript(
        after_pause, "erfundener Muell", protected_prefix=floor
    )
    assert survived.startswith(floor), (
        f"the dictation before the pause was destroyed: {survived!r}"
    )


def test_the_protected_prefix_also_survives_a_recased_transcript():
    """The other direction: a raw prefix check alone would miss this."""
    recased = merge_rolling_window_transcript(
        "hallo welt und weiter", "voellig anderes", protected_prefix="Hallo Welt"
    )
    assert recased == "Hallo Welt voellig anderes"
