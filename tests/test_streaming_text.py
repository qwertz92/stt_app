import pytest

from stt_app.config import STREAMING_REVISION_WORD_WINDOW, STREAMING_STABLE_WORD_GUARD
from stt_app.streaming_text import (
    StreamingTextState,
    append_only_stream_extension_tail,
    append_only_stream_finalize_tail,
    append_only_stream_partial_candidate,
    merge_rolling_window,
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

    assert len(accumulated.split()) <= len(["so", "this", "is", "the", "real", "dictation", "i", "spoke"]) + 5


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


def test_the_protected_prefix_is_spliced_on_at_the_words_it_shares():
    """The partial overlap, which the whole-floor check above cannot see.

    The window is the trailing few seconds of audio, so one straddling the
    measured pause re-decodes the floor's last words before the new speech. It
    does not *contain* the floor, so the containment check passes it through,
    and welding it on whole repeated those words in the transcript that gets
    pasted -- measured with a real floor ending "gesprochene Sprache um." and a
    window opening with the same three words.
    """
    floor = "das system wandelt gesprochene sprache um"
    previous = f"{floor} danach folgt ein weiterer satz"

    assert (
        merge_rolling_window_transcript(
            previous,
            "gesprochene sprache um und jetzt kommt etwas neues",
            protected_prefix=floor,
        )
        == f"{floor} und jetzt kommt etwas neues"
    )


def test_a_window_boundary_that_cut_the_first_word_still_splices_onto_the_floor():
    """The floor branch needs the same re-anchoring as the alignment above it.

    `_suffix_prefix_overlap_len` anchors at the window's first word, which is
    exactly the word the window boundary cut in half, so without skipping past
    it one mistranscribed fragment brings the duplication back.
    """
    floor = "das system wandelt gesprochene sprache um"
    previous = f"{floor} danach folgt ein weiterer satz"

    assert (
        merge_rolling_window_transcript(
            previous,
            "ochene sprache um und jetzt kommt etwas neues",
            protected_prefix=floor,
        )
        == f"{floor} und jetzt kommt etwas neues"
    )


def test_a_window_sharing_nothing_with_the_floor_is_still_joined_whole():
    """Splicing must not become a reason to drop a window that does not fit."""
    floor = "das system wandelt gesprochene sprache um"
    previous = f"{floor} danach folgt ein weiterer satz"

    assert (
        merge_rolling_window_transcript(
            previous,
            "voellig andere woerter ohne jede ueberlappung",
            protected_prefix=floor,
        )
        == f"{floor} voellig andere woerter ohne jede ueberlappung"
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


def test_the_merge_reports_which_branch_resolved_it():
    """`aligned` is a public contract now, so pin it directly.

    The caller decides whether to pin the floor from this flag. Inferring it
    from `text.startswith(previous)` was wrong in both directions -- once a
    floor exists the replace branch also returns text starting with
    `previous`, which is what let hallucinations be pinned permanently.
    """
    from stt_app.streaming_text import merge_rolling_window

    # Overlapping windows: aligned.
    aligned = merge_rolling_window("das ist der erste", "der erste teil davon")
    assert aligned.aligned is True
    assert aligned.text == "das ist der erste teil davon"

    # A pause: appended on trust, corroborated by nothing.
    appended = merge_rolling_window("erster teil", "ganz neuer text", new_segment=True)
    assert appended.aligned is False
    assert appended.text == "erster teil ganz neuer text"

    # Unalignable with a floor: bounded replace, and NOT aligned even though
    # the result starts with `previous` -- the case the old inference missed.
    floor = "erster teil"
    replaced = merge_rolling_window(
        floor, "voellig anderes fenster", protected_prefix=floor
    )
    assert replaced.aligned is False
    assert replaced.text.startswith(floor), (
        "precondition: the bounded replace does start with previous"
    )

    # The three remaining replace exits. Each is the shape of the bug this
    # flag exists to prevent -- a replace mistaken for corroboration pins
    # junk permanently -- and mutation testing showed all three unasserted.
    no_floor = merge_rolling_window("erster teil", "voellig anderes")
    assert no_floor.aligned is False, "a replace with no floor is not alignment"

    mismatched = merge_rolling_window(
        "erster teil", "voellig anderes", protected_prefix="etwas anderes"
    )
    assert mismatched.aligned is False
    assert mismatched.text == "voellig anderes"

    already_contained = merge_rolling_window(
        "alpha beta gamma delta", "alpha beta", protected_prefix="alpha beta"
    )
    assert already_contained.aligned is False
    assert already_contained.text == "alpha beta"

    # The re-anchored overlap IS alignment: the seam was found a few words
    # into the window, past the boundary that cut a word in half.
    reanchored = merge_rolling_window(
        "das ist der erste teil", "XX der erste teil davon"
    )
    assert reanchored.aligned is True
    assert reanchored.text == "das ist der erste teil davon"

    # An empty window changes nothing. It reports aligned because nothing was
    # contradicted, which is exactly why a caller must also require growth --
    # an empty decode corroborates nothing.
    empty = merge_rolling_window("erster teil", "")
    assert empty.text == "erster teil"
    assert empty.aligned is True


_FLOOR = "das protokoll der letzten sitzung ist noch nicht fertig"
_DRIFTED = f"{_FLOOR} voellig anderer erfundener text hier"


@pytest.mark.parametrize("junk_words", [0, 1, 2, 3, 4])
def test_the_floor_splice_reaches_past_the_boundary_skip(junk_words):
    """Past three junk words the splice used to fall through to a blind weld.

    The floor branch is reached because `previous` has already drifted beyond
    rescue, so the window's head can hold several words of that drift -- not
    just the one word the window boundary cut in half, which is what
    `_WINDOW_BOUNDARY_SKIP_WORDS` describes. Bounding the floor splice by it
    meant the fourth junk word turned the bounded replace into an unbounded
    append: measured 19 words where 11 were right, with the floor's last four
    re-emitted after the junk, and pasted into the document.
    """
    junk = " ".join(f"j{index}" for index in range(junk_words))
    window = f"{junk} ist noch nicht fertig und wird".strip()

    assert (
        merge_rolling_window_transcript(_DRIFTED, window, protected_prefix=_FLOOR)
        == f"{_FLOOR} und wird"
    )


@pytest.mark.parametrize("junk_words", [5, 6, 7])
def test_a_seam_that_discards_more_than_it_explains_is_refused(junk_words):
    """The bound is deliberate, and duplicating is the safe side of it.

    A candidate past `_WINDOW_BOUNDARY_SKIP_WORDS` must explain at least as
    much as it throws away, so a four-word seam behind five junk words is
    refused and the window is welded on whole -- the original behaviour, junk
    and all. Accepting it instead is what let a two-word coincidence at skip 5
    swallow an entire window of real speech. Bounded junk beats lost text.
    """
    junk = " ".join(f"j{index}" for index in range(junk_words))
    window = f"{junk} ist noch nicht fertig und wird".strip()

    merged = merge_rolling_window_transcript(
        _DRIFTED, window, protected_prefix=_FLOOR
    )
    assert merged.startswith(_FLOOR), merged
    assert merged.endswith("und wird"), merged
    assert junk in merged, "the window was dropped instead of welded on"


def test_a_deep_coincidence_never_beats_a_shallow_real_seam():
    """The other direction, and the reason the score subtracts the skip.

    Taking the longest overlap inverts the first-match defect: here the real
    two-word seam sits at skip 2 and a three-word coincidence at skip 7, and
    preferring the longer one dropped "ein neuer gedanke" from the transcript.
    `overlap - skip` scores them 0 against -4.
    """
    floor = "der bericht ist lang und das ist"
    previous = f"{floor} erfundener text hier"
    window = "j0 j1 das ist ein neuer gedanke und das ist gut"

    assert (
        merge_rolling_window_transcript(previous, window, protected_prefix=floor)
        == f"{floor} ein neuer gedanke und das ist gut"
    )


def test_a_deep_coincidence_cannot_swallow_a_whole_window():
    """The worst shape: the seam leaves no window words at all.

    The floor's last two words recur at the very end of the window, so the
    deepest candidate explains two words and discards five -- and the window
    contributed nothing, with five real words gone and the transcript not
    advancing at all.
    """
    floor = "und und und"
    previous = f"{floor} erfundener text hier"
    window = "dann dann dann dann dann und und"

    assert (
        merge_rolling_window_transcript(previous, window, protected_prefix=floor)
        == f"{floor} {window}"
    )


def test_a_seam_of_pure_punctuation_is_not_agreement():
    """Two windows agreeing on "..." have not agreed on anything.

    `_stream_word_key` strips `.,;:!?)]}`, so every punctuation-only token
    keys to the empty string and they all match each other. Two of them
    cleared the two-word overlap threshold, and the merge reads a cleared
    threshold as the corroboration it may advance the floor on.
    """
    resolved = merge_rolling_window(
        "ich habe gesagt ... !", ": . und dann kam etwas ganz anderes"
    )
    assert resolved.aligned is False


def test_one_real_word_in_the_seam_is_still_a_seam():
    """The punctuation gate must not be able to cause a replace.

    Counting substantive words *against* the two-word threshold is stricter
    than the threshold has ever been: an overlap of "praktisch ..." scores
    one, fails, and the merge falls through to the replace -- and before the
    first measured pause there is no floor to bound that, so the whole
    dictation so far is gone, not one window. Measured at 13 words. The rule
    is the token threshold as before, plus at least one real word.
    """
    previous = (
        "die spracherkennung wandelt sprache in text um und das ist sehr "
        "praktisch ..."
    )

    merged = merge_rolling_window_transcript(
        previous, "praktisch ... und jetzt kommt der naechste satz"
    )

    assert merged.startswith("die spracherkennung wandelt sprache"), merged
    assert merged.endswith("und jetzt kommt der naechste satz"), merged


def test_a_seam_containing_punctuation_still_counts_its_real_words():
    """The other direction, and why the empty key is counted, not refused.

    Making the mark itself non-matching would break a genuine three-word seam
    down to no overlap at all, because `_suffix_prefix_overlap_len` needs
    every word of the slice to match.
    """
    assert (
        merge_rolling_window_transcript(
            "der erste teil hallo ... welt", "hallo ... welt und weiter"
        )
        == "der erste teil hallo ... welt und weiter"
    )


def _remote_state() -> StreamingTextState:
    return StreamingTextState(
        stable_word_guard=STREAMING_STABLE_WORD_GUARD,
        revision_word_window=STREAMING_REVISION_WORD_WINDOW,
    )


def test_a_revised_punctuation_mark_does_not_freeze_remote_live_insertion():
    """The two callers mean opposite things by a suffix/prefix overlap.

    `merge_rolling_window` passes a trailing window of the same audio, where
    the overlap is the seam. `StreamingTextState` passes the provider's
    whole-session text, whose head is the start of the dictation -- so an
    overlap there is a coincidence, and `min_overlap_words` is all that stands
    in its way. Relaxing that to "two tokens, one of them real" let a revised
    sentence-final mark through: every mark keys to the empty string and
    matches every other, the revised-away mark was welded in verbatim, and
    `committed_text` then stopped being a prefix of anything the provider would
    send again. Live insertion froze for the rest of the session, the stop-time
    recovery returned nothing, and the overlay still showed the right
    transcript, so nothing reported it. Measured: three words never pasted.
    """
    partials = [
        "Ja",
        "Ja ...",
        "Ja . wir",
        "Ja . wir muessen das",
        "Ja . wir muessen das noch pruefen bevor wir das freigeben",
    ]
    state = _remote_state()
    document = ""
    for partial in partials:
        document += state.apply_partial_append_only(partial).insertion
    tail = append_only_stream_finalize_tail(
        state.committed_text, partials[-1], state.last_partial_text
    )
    document += stream_insertion_text(state.committed_text, tail)

    assert "".join(document.split()) == "".join(partials[-1].split()), (
        f"the document is not the full dictation: {document!r}"
    )


def test_a_trailing_window_still_merges_on_one_real_word():
    """The other half of the split: the rolling window keeps the relaxed gate.

    Requiring two real words here refuses a seam of "praktisch ...", and a
    refused merge before the first measured pause replaces the whole
    accumulated text -- 11 of 13 words gone, measured.
    """
    previous = (
        "die spracherkennung wandelt sprache in text um und das ist sehr "
        "praktisch ..."
    )

    merged = merge_rolling_window_transcript(
        previous, "praktisch ... und jetzt kommt der naechste satz"
    )

    assert merged.startswith("die spracherkennung wandelt sprache"), merged
    assert merged.endswith("und jetzt kommt der naechste satz"), merged


def test_the_default_caller_semantics_are_the_safe_ones():
    """A new caller that forgets the flag gets the strict threshold.

    The permissive branch is only correct for audio-window text, so the
    parameter defaults to the whole-text reading rather than the other way
    round.
    """
    assert (
        append_only_stream_partial_candidate("Ja ...", "Ja . wir")
        == "Ja . wir"
    )
    assert (
        append_only_stream_partial_candidate(
            "Ja ...", "Ja . wir", current_is_a_trailing_window=True
        )
        == "Ja ... wir"
    )


def test_the_floor_splice_also_reads_the_window_as_a_window():
    """The floor branch passes a trailing window too, and must say so.

    It splices the *floor* against the same audio window, so a seam of one
    real word plus a mark is as genuine there as in the alignment above it.
    Reading it as whole-session text refuses the splice and the window is
    welded on whole, which repeats the words the seam had matched.
    """
    floor = "der bericht ueber die sitzung ist praktisch ..."
    drifted = f"{floor} voellig erfundener text hier"

    merged = merge_rolling_window(
        drifted, "praktisch ... und jetzt kommt der rest", protected_prefix=floor
    )

    assert merged.text == f"{floor} und jetzt kommt der rest", merged.text
