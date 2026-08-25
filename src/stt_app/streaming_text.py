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
    if overlap >= max(1, int(min_overlap_words)):
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
    for skip in range(1, _WINDOW_BOUNDARY_SKIP_WORDS + 1):
        if skip >= len(current_words):
            break
        overlap = _suffix_prefix_overlap_len(previous_words, current_words[skip:])
        if overlap >= required_overlap:
            joined = " ".join(
                previous_words + current_words[skip + overlap:]
            ).strip()
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


def _stream_word_key(word: str) -> str:
    return word.strip().strip(".,;:!?)]}").lower()
