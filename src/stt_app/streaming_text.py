from __future__ import annotations

from dataclasses import dataclass

_NO_SPACE_BEFORE = {".", ",", ";", ":", "!", "?", ")", "]", "}"}

# How far into a rolling audio window the overlap search may re-anchor. The
# window boundary cuts a word in half, so its first token or two are the ones
# the model is most likely to get wrong; beyond that a "match" is more likely
# to be a coincidental repeat than the real seam.
_WINDOW_BOUNDARY_SKIP_WORDS = 3


@dataclass(frozen=True, slots=True)
class StreamingTextAppend:
    insertion: str
    display_text: str


@dataclass(slots=True)
class StreamingTextState:
    stable_word_guard: int
    revision_word_window: int
    committed_text: str = ""
    live_text: str = ""
    last_partial_text: str = ""

    def reset(self) -> None:
        self.committed_text = ""
        self.live_text = ""
        self.last_partial_text = ""

    def apply_partial_append_only(self, partial_text: str) -> StreamingTextAppend:
        text = normalize_stream_text(partial_text)
        candidate_text = append_only_stream_partial_candidate(
            self.last_partial_text,
            text,
            min_overlap_words=max(
                2,
                self.stable_word_guard + self.revision_word_window,
            ),
        )
        previous_partial = self.last_partial_text
        previous_committed = self.committed_text
        # Deliberately NOT joining a candidate that has lost the committed
        # prefix onto it. That does unfreeze insertion (once the prefix is
        # gone, compute_stream_locked_prefix can never advance again), but a
        # provider that revises a word inside the already-pasted region then
        # re-emits the whole dictation: measured at 86 pasted words for a
        # 48-word truth on an AssemblyAI turn revision, scaling with session
        # length. Pasting the transcript twice is worse than stopping early.
        next_committed = compute_stream_locked_prefix(
            previous_committed,
            previous_partial,
            candidate_text,
            stable_word_guard=self.stable_word_guard,
            revision_word_window=self.revision_word_window,
        )
        tail = append_only_stream_extension_tail(previous_committed, next_committed)
        insertion = stream_insertion_text(previous_committed, tail)
        self.last_partial_text = candidate_text
        self.committed_text = next_committed
        self.live_text = candidate_text
        return StreamingTextAppend(
            insertion=insertion,
            display_text=candidate_text,
        )

    def rollback_commit(self, previous_committed: str) -> None:
        """Undo a commit whose text never reached the target window.

        `apply_partial_append_only` marks text as committed the moment it is
        handed to the inserter. If that paste fails the text is *not* in the
        document, so the commit has to be taken back or the words are lost for
        good: the locked prefix would never offer them again.
        """
        self.committed_text = previous_committed

    def finalize_append_only(self, final_text: str) -> tuple[str, str]:
        normalized_final = normalize_stream_text(final_text)
        tail = append_only_stream_finalize_tail(
            self.committed_text,
            normalized_final,
            self.last_partial_text,
        )
        insertion = stream_insertion_text(self.committed_text, tail)
        self.live_text = stream_join_text(self.committed_text, tail)
        self.committed_text = self.live_text
        return insertion, normalized_final


def normalize_stream_text(text: str) -> str:
    tokens = str(text or "").strip().split()
    return " ".join(tokens).strip()


def stream_insertion_text(committed: str, tail: str) -> str:
    new_part = normalize_stream_text(tail)
    if not new_part:
        return ""
    if not normalize_stream_text(committed):
        return new_part
    if new_part[:1] in _NO_SPACE_BEFORE:
        return new_part
    return f" {new_part}"


def stream_join_text(committed: str, tail: str) -> str:
    base = normalize_stream_text(committed)
    insertion = stream_insertion_text(base, tail)
    combined = f"{base}{insertion}"
    return normalize_stream_text(combined)


def split_stream_words(text: str) -> list[str]:
    normalized = normalize_stream_text(text)
    if not normalized:
        return []
    return normalized.split(" ")


def common_prefix_len(left: list[str], right: list[str]) -> int:
    size = min(len(left), len(right))
    for idx in range(size):
        if left[idx].lower() != right[idx].lower():
            return idx
    return size


def append_only_stream_partial_candidate(
    previous_text: str,
    current_text: str,
    *,
    min_overlap_words: int = 2,
) -> str:
    previous = normalize_stream_text(previous_text)
    current = normalize_stream_text(current_text)
    if not previous or not current:
        return current

    previous_words = split_stream_words(previous)
    current_words = split_stream_words(current)
    if common_prefix_len(previous_words, current_words) == len(previous_words):
        return current

    overlap = _suffix_prefix_overlap_len(previous_words, current_words)
    if _substantive_word_count(current_words[:overlap]) >= max(
        1, int(min_overlap_words)
    ):
        merged = previous_words + current_words[overlap:]
        return " ".join(merged).strip()
    return current


def stream_text_extends(base: str, candidate: str) -> bool:
    """Report whether `candidate` still contains `base` as a word prefix."""
    base_words = split_stream_words(base)
    if not base_words:
        return True
    candidate_words = split_stream_words(candidate)
    return common_prefix_len(base_words, candidate_words) == len(base_words)


@dataclass(frozen=True, slots=True)
class RollingMergeResult:
    """How a rolling window was merged, not just the resulting text.

    The caller has to know whether the window ALIGNED with what came before
    or merely replaced it. Inferring that from `text.startswith(previous)`
    is wrong in both directions: once a floor exists the replace branch also
    returns something starting with `previous`, and the word-based keep
    branch may re-case a word so the raw prefix no longer matches.
    """

    text: str
    aligned: bool


def _join_at_seam(
    base_words: list[str],
    current_words: list[str],
    required_overlap: int,
    *,
    max_skip: int = _WINDOW_BOUNDARY_SKIP_WORDS,
) -> str | None:
    """Join a window onto `base_words` at the words they share, or `None`.

    `_suffix_prefix_overlap_len` anchors every candidate at the window's first
    word -- the word the window boundary cut in half -- so one mistranscribed
    fragment there defeats the search. Re-anchoring up to `max_skip` words in
    finds the seam anyway.

    It takes the LONGEST seam, not the first one over the threshold. Returning
    early on the first match let a coincidental short overlap near the window's
    head beat the real seam a word or two further in, and everything between
    the two was then emitted twice -- reported as `aligned=True`, so the caller
    pinned the duplicate as the new floor. Measured on a base ending "... sechs
    sieben acht" against a window "x sieben acht drei vier fuenf sechs sieben
    acht neun zehn": skip 1 overlaps 2 words and skip 3 overlaps 6, and taking
    skip 1 repeated six words. Equal overlaps keep the smallest skip, which
    drops the fewest window words, and this is also what `skip == 0` already
    does in `append_only_stream_partial_candidate`.
    """
    best_skip = 0
    best_overlap = 0
    for skip in range(1, min(max_skip, len(current_words) - 1) + 1):
        overlap = _suffix_prefix_overlap_len(base_words, current_words[skip:])
        if overlap <= best_overlap:
            continue
        if (
            _substantive_word_count(current_words[skip : skip + overlap])
            < required_overlap
        ):
            continue
        best_skip = skip
        best_overlap = overlap
    if not best_overlap:
        return None
    return " ".join(
        base_words + current_words[best_skip + best_overlap :]
    ).strip()


def merge_rolling_window(
    previous_text: str,
    current_text: str,
    *,
    min_overlap_words: int = 2,
    new_segment: bool = False,
    protected_prefix: str = "",
) -> RollingMergeResult:
    """`merge_rolling_window_transcript`, plus how it resolved."""
    previous = normalize_stream_text(previous_text)
    current = normalize_stream_text(current_text)
    # An empty window (trailing silence, or one that decodes to nothing) is
    # not a correction and must not wipe the accumulated text. Reported as
    # aligned only in the sense that nothing was contradicted -- callers that
    # act on `aligned` must also require growth, because an empty decode
    # corroborates nothing.
    if not previous or not current:
        return RollingMergeResult(current or previous, aligned=bool(previous))
    if new_segment:
        # A measured pause longer than the window: this audio shares nothing
        # with what came before, so there is no seam to find. Appended on
        # trust, and reported as NOT aligned -- nothing corroborated it.
        return RollingMergeResult(
            stream_join_text(previous, current), aligned=False
        )

    merged = append_only_stream_partial_candidate(
        previous, current, min_overlap_words=min_overlap_words
    )
    if stream_text_extends(previous, merged):
        return RollingMergeResult(merged, aligned=True)

    previous_words = split_stream_words(previous)
    current_words = split_stream_words(current)
    required_overlap = max(1, int(min_overlap_words))
    joined = _join_at_seam(previous_words, current_words, required_overlap)
    if joined is not None:
        return RollingMergeResult(joined, aligned=True)

    # Unalignable: the window replaces the accumulated text, bounded by the
    # floor. What that discards is what the last window that ADDED text
    # contributed -- not what the current one did, which added nothing,
    # which is why the floor stalled. Up to one full window of speech.
    protected = normalize_stream_text(protected_prefix)
    if not protected:
        return RollingMergeResult(current, aligned=False)
    # Either comparison is enough, and BOTH are needed. Word-only was tried
    # and reverted: `stream_join_text` welds leading punctuation onto the
    # previous word, so the floor's last word gains a "." on the very call
    # that pins it, the word check fails, and the whole dictation is
    # replaced. Raw-only misses a provider that re-cases a committed word.
    if not (
        previous.startswith(protected)
        or stream_text_extends(protected, previous)
    ):
        return RollingMergeResult(current, aligned=False)
    # The window may already contain the floor (a provider that re-emits from
    # the start). Joining then duplicates it.
    if stream_text_extends(protected, current):
        return RollingMergeResult(current, aligned=False)
    # The window is the trailing few seconds of audio, so one that straddles
    # the measured pause re-decodes the floor's last words before the new
    # speech. Welding it on whole then repeated them in the pasted transcript:
    # for a floor ending "gesprochene Sprache um." and a window starting with
    # the same three words, the merge produced that clause twice. Splice at the
    # seam instead -- append-only, so nothing the floor holds can be lost, and
    # still `aligned=False` because agreeing with the floor is not the two
    # overlapping windows that are allowed to advance it.
    spliced = append_only_stream_partial_candidate(
        protected, current, min_overlap_words=min_overlap_words
    )
    if stream_text_extends(protected, spliced):
        return RollingMergeResult(spliced, aligned=False)
    # The seam search here is not bounded by `_WINDOW_BOUNDARY_SKIP_WORDS`,
    # unlike the alignment against `previous` above, and the asymmetry is
    # deliberate. That bound describes one word cut in half at the window
    # boundary; here `previous` has already been discarded as unalignable, so
    # the window's head can hold several words of the same drift -- and past
    # the fourth of them the splice fell through to the blind append below,
    # which re-emitted the floor's tail. Measured on a floor ending "ist noch
    # nicht fertig" against a window "j0 j1 j2 j3 ist noch nicht fertig und
    # wird": 11 words up to three junk words, 19 from four. Widening it is
    # safe in a way it would not be above, because the floor is preserved
    # verbatim either way, the result stays `aligned=False` so no floor is
    # advanced by it, and the required two substantive words of overlap are
    # what separate a real seam from a coincidence.
    spliced_past_boundary = _join_at_seam(
        split_stream_words(protected),
        current_words,
        required_overlap,
        max_skip=len(current_words),
    )
    if spliced_past_boundary is not None:
        return RollingMergeResult(spliced_past_boundary, aligned=False)
    return RollingMergeResult(stream_join_text(protected, current), aligned=False)

def merge_rolling_window_transcript(
    previous_text: str,
    current_text: str,
    *,
    min_overlap_words: int = 2,
    new_segment: bool = False,
    protected_prefix: str = "",
) -> str:
    """Merge a trailing-audio-window transcript into the accumulated text.

    `current_text` describes only the last few seconds of audio, never the whole
    utterance, which changes two things versus a provider's full-text revision:

    - An empty window (trailing silence, or one that simply decodes to nothing)
      is not a correction and must not wipe the accumulated text. Letting it
      through produced an *empty final transcript* for a whole dictation at the
      fast finalization path.
    - The window boundary routinely cuts a word in half, and
      `_suffix_prefix_overlap_len` anchors every candidate alignment at the
      window's *first* word, so a mistranscription of that one fragment
      defeated the whole search. Retrying the alignment a few words into the
      window recovers it.

    ``new_segment`` marks a window that follows a pause longer than the window
    itself, so it cannot overlap the accumulated text and is appended instead.

    A window that still cannot be aligned falls back to replacing the
    accumulated text, but only back to ``protected_prefix``: text closed off
    by an earlier measured pause is never destroyed by a later window.
    Replacing loses text, but the alternative — appending — is
    worse: a silent microphone makes the model emit a fresh hallucination every
    partial, none of which can ever align, so an append fallback grows without
    bound and types hundreds of junk words into the user's document at
    finalization (measured: 896 words after two minutes of silence).
    """
    return merge_rolling_window(
        previous_text,
        current_text,
        min_overlap_words=min_overlap_words,
        new_segment=new_segment,
        protected_prefix=protected_prefix,
    ).text

def compute_stream_locked_prefix(
    committed: str,
    previous_partial: str,
    current_partial: str,
    *,
    stable_word_guard: int,
    revision_word_window: int,
) -> str:
    committed_words = split_stream_words(committed)
    previous_words = split_stream_words(previous_partial)
    current_words = split_stream_words(current_partial)
    if not current_words or not previous_words:
        return normalize_stream_text(committed)

    stable_len = common_prefix_len(previous_words, current_words)
    guard = max(0, int(stable_word_guard))
    revision_window = max(0, int(revision_word_window))
    locked_len = max(0, stable_len - guard - revision_window)
    if locked_len <= len(committed_words):
        return normalize_stream_text(committed)

    candidate_words = current_words[:locked_len]
    if common_prefix_len(committed_words, candidate_words) < len(committed_words):
        return normalize_stream_text(committed)
    return " ".join(candidate_words).strip()


def append_only_stream_extension_tail(committed: str, candidate: str) -> str:
    committed_words = split_stream_words(committed)
    candidate_words = split_stream_words(candidate)
    if not candidate_words:
        return ""
    if not committed_words:
        return " ".join(candidate_words).strip()
    prefix_len = common_prefix_len(committed_words, candidate_words)
    if prefix_len < len(committed_words):
        return ""
    return " ".join(candidate_words[prefix_len:]).strip()


def append_only_stream_finalize_tail(
    committed: str,
    final_text: str,
    last_partial_text: str,
) -> str:
    normalized_final = normalize_stream_text(final_text)
    if normalized_final:
        return append_only_stream_extension_tail(committed, normalized_final)
    return append_only_stream_extension_tail(committed, last_partial_text)


def _suffix_prefix_overlap_len(left: list[str], right: list[str]) -> int:
    max_size = min(len(left), len(right))
    for size in range(max_size, 0, -1):
        left_tail = left[-size:]
        right_head = right[:size]
        # Equal length by construction: both are `size`-long slices.
        if all(
            _stream_words_match(a, b)
            for a, b in zip(left_tail, right_head, strict=True)
        ):
            return size
    return 0


def _stream_words_match(left: str, right: str) -> bool:
    return _stream_word_key(left) == _stream_word_key(right)


def _substantive_word_count(words: list[str]) -> int:
    """How many of `words` carry a word, rather than only punctuation.

    `_stream_word_key` strips `.,;:!?)]}` from both ends, so "...", "!", "?!"
    and ":" all key to the empty string and therefore match each other. Two
    such tokens cleared a two-word overlap threshold with no lexical
    agreement at all, and the merge treats a cleared threshold as "two
    overlapping windows agreed on the seam" -- the corroboration it is
    allowed to advance the floor on.

    Counted rather than refused in `_stream_words_match`: a genuine seam may
    contain a standalone mark ("hallo ... welt"), and making the mark itself
    non-matching would break the whole three-word overlap down to nothing
    instead of scoring it as the two real words it is.
    """
    return sum(1 for word in words if _stream_word_key(word))


def _stream_word_key(word: str) -> str:
    return word.strip().strip(".,;:!?)]}").lower()
