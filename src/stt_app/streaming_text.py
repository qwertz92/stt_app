from __future__ import annotations

from dataclasses import dataclass

_NO_SPACE_BEFORE = {".", ",", ";", ":", "!", "?", ")", "]", "}"}


@dataclass(frozen=True, slots=True)
class StreamingTextReplacement:
    current_insertion: str
    desired_insertion: str


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

    def apply_partial(self, partial_text: str) -> StreamingTextReplacement:
        text = normalize_stream_text(partial_text)
        previous_partial = self.last_partial_text
        next_committed = compute_stream_locked_prefix(
            self.committed_text,
            previous_partial,
            text,
            stable_word_guard=self.stable_word_guard,
            revision_word_window=self.revision_word_window,
        )
        current_tail = best_stream_extension_tail(next_committed, self.live_text)
        desired_tail = best_stream_extension_tail(next_committed, text)
        replacement = StreamingTextReplacement(
            current_insertion=stream_insertion_text(next_committed, current_tail),
            desired_insertion=stream_insertion_text(next_committed, desired_tail),
        )
        self.last_partial_text = text
        self.committed_text = next_committed
        self.live_text = stream_join_text(next_committed, desired_tail)
        return replacement

    def finalize(self, final_text: str) -> tuple[StreamingTextReplacement, str]:
        normalized_final = normalize_stream_text(final_text)
        current_tail = best_stream_extension_tail(
            self.committed_text,
            self.live_text,
        )
        desired_tail = best_stream_finalize_tail(
            self.committed_text,
            normalized_final,
            self.last_partial_text,
        )
        replacement = StreamingTextReplacement(
            current_insertion=stream_insertion_text(
                self.committed_text,
                current_tail,
            ),
            desired_insertion=stream_insertion_text(
                self.committed_text,
                desired_tail,
            ),
        )
        self.live_text = stream_join_text(self.committed_text, desired_tail)
        return replacement, normalized_final


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


def suffix_prefix_overlap_len(left: list[str], right: list[str]) -> int:
    if not left or not right:
        return 0
    max_size = min(len(left), len(right))
    overlap = 0
    for size in range(1, max_size + 1):
        if [token.lower() for token in left[-size:]] == [
            token.lower() for token in right[:size]
        ]:
            overlap = size
    return overlap


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


def best_stream_extension_tail(committed: str, candidate: str) -> str:
    committed_words = split_stream_words(committed)
    candidate_words = split_stream_words(candidate)
    if not candidate_words:
        return ""
    prefix_len = common_prefix_len(committed_words, candidate_words)
    candidate_tail = candidate_words[prefix_len:]
    committed_tail = committed_words[prefix_len:]
    overlap_len = suffix_prefix_overlap_len(committed_tail, candidate_tail)
    delta_words = candidate_tail[overlap_len:]
    return " ".join(delta_words).strip()


def compute_stream_live_delta(
    committed: str,
    previous_partial: str,
    current_partial: str,
    *,
    stable_word_guard: int,
    revision_word_window: int,
) -> tuple[str, str]:
    next_committed = compute_stream_locked_prefix(
        committed,
        previous_partial,
        current_partial,
        stable_word_guard=stable_word_guard,
        revision_word_window=revision_word_window,
    )
    return best_stream_extension_tail(next_committed, current_partial), next_committed


def best_stream_finalize_tail(
    committed: str,
    final_text: str,
    last_partial_text: str,
) -> str:
    committed_words = split_stream_words(committed)
    best_tail = ""
    best_score = -1
    for candidate in (final_text, last_partial_text):
        candidate_words = split_stream_words(candidate)
        if not candidate_words:
            continue
        prefix_len = common_prefix_len(committed_words, candidate_words)
        candidate_tail = candidate_words[prefix_len:]
        committed_tail = committed_words[prefix_len:]
        overlap_len = suffix_prefix_overlap_len(committed_tail, candidate_tail)
        delta_words = candidate_tail[overlap_len:]
        score = prefix_len + overlap_len
        if prefix_len < len(committed_words) and overlap_len == 0:
            score -= 1
        if score > best_score:
            best_score = score
            best_tail = " ".join(delta_words).strip()
    return best_tail
