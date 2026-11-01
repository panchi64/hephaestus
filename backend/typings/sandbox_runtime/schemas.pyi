from dataclasses import dataclass, field

@dataclass
class FsReadRestrictionConfig:
    deny_only: list[str] = field(default_factory=list)

@dataclass
class FsWriteRestrictionConfig:
    allow_only: list[str] = field(default_factory=list)
    deny_within_allow: list[str] = field(default_factory=list)
