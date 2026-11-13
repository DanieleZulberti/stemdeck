from __future__ import annotations

from dataclasses import dataclass
from typing import Type


class SemanticAudioType:
    """Base class for semantic audio content types."""


class UnknownAudio(SemanticAudioType):
    pass


class Mixture(SemanticAudioType):
    pass


class Vocals(SemanticAudioType):
    pass


class LeadVocals(Vocals):
    pass


class BackingVocals(Vocals):
    pass


class Choir(Vocals):
    pass


class Instrument(SemanticAudioType):
    pass


class Guitar(Instrument):
    pass


class LeadGuitar(Guitar):
    pass


class RhythmGuitar(Guitar):
    pass


class Bass(Instrument):
    pass


class Drums(Instrument):
    pass


class Keys(Instrument):
    pass


class Piano(Keys):
    pass


class Keyboard(Keys):
    pass


SEMANTIC_TYPES: dict[str, Type[SemanticAudioType]] = {
    "unknown_audio": UnknownAudio,
    "mixture": Mixture,
    "vocals": Vocals,
    "lead_vocals": LeadVocals,
    "backing_vocals": BackingVocals,
    "choir": Choir,
    "instrument": Instrument,
    "guitar": Guitar,
    "lead_guitar": LeadGuitar,
    "rhythm_guitar": RhythmGuitar,
    "bass": Bass,
    "drums": Drums,
    "keys": Keys,
    "piano": Piano,
    "keyboard": Keyboard,
}


def resolve_semantic_type(name: str) -> Type[SemanticAudioType]:
    try:
        return SEMANTIC_TYPES[name]
    except KeyError as exc:
        known = ", ".join(sorted(SEMANTIC_TYPES))
        raise ValueError(f"Unknown semantic type '{name}'. Known types: {known}") from exc


def is_semantically_compatible(actual: str, expected: str) -> bool:
    """Return True when actual is expected or a semantic subtype of expected."""
    return issubclass(resolve_semantic_type(actual), resolve_semantic_type(expected))


@dataclass(frozen=True)
class SemanticCompatibility:
    actual: str
    expected: str
    compatible: bool
