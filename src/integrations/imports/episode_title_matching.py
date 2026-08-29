import re

_PART_WORD_TO_NUMBER = {
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
}


def _normalize_part_numbers(text):
    """Normalize "Part One" style wording to "Part 1" for comparison."""
    result = text
    for word, number in _PART_WORD_TO_NUMBER.items():
        result = re.sub(
            rf"\bpart\s+{word}\b",
            f"part {number}",
            result,
            flags=re.IGNORECASE,
        )
    return result


def _exact_match(title, title_parts, name, name_parts):  # noqa: ARG001
    return name == title


def _normalized_part_match(title, title_parts, name, name_parts):  # noqa: ARG001
    return name_parts == title_parts


def _substring_match(title, title_parts, name, name_parts):  # noqa: ARG001
    return name in title or title in name


def _normalized_part_substring_match(title, title_parts, name, name_parts):  # noqa: ARG001
    return name_parts in title_parts or title_parts in name_parts


def _truncated_name_match(title, title_parts, name, name_parts):  # noqa: ARG001
    if not name.endswith(("...", "…")):
        return False
    prefix = name.rstrip(".…").rstrip()
    return bool(prefix) and title.startswith(prefix)


_MATCH_STRATEGIES = (
    _exact_match,
    _normalized_part_match,
    _substring_match,
    _normalized_part_substring_match,
    _truncated_name_match,
)


def match_episode_number(episode_title, episodes):
    """Fuzzy-match a viewing-history episode title against TMDB episodes.

    `episodes` is the `episodes` list from a TMDB season metadata payload,
    each with `name` and `episode_number`. Tries, in order: an exact name
    match, a match after normalizing "Part One"/"Part 1" wording, a
    substring match in either direction, and a match against TMDB names
    truncated with an ellipsis. Returns the episode_number or None.
    """
    if not episode_title or not episodes:
        return None

    title = episode_title.strip().lower()
    title_parts = _normalize_part_numbers(title)

    names = []
    for episode in episodes:
        name = (episode.get("name") or "").strip().lower()
        if name:
            names.append((episode, name, _normalize_part_numbers(name)))

    for strategy in _MATCH_STRATEGIES:
        for episode, name, name_parts in names:
            if strategy(title, title_parts, name, name_parts):
                return episode["episode_number"]

    return None
