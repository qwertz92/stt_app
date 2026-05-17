from __future__ import annotations

from dataclasses import dataclass

_NO_SPACE_BEFORE = {".", ",", ";", ":", "!", "?", ")", "]", "}"}


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
        if all(_stream_words_match(a, b) for a, b in zip(left_tail, right_head)):
            return size
    return 0


def _stream_words_match(left: str, right: str) -> bool:
    return _stream_word_key(left) == _stream_word_key(right)


def _stream_word_key(word: str) -> str:
    return word.strip().strip(".,;:!?)]}").lower()
