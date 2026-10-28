from dataclasses import dataclass, field


@dataclass
class Doc:
    id: str          # "yt:<source>:<videoId>" | "docs:<source>:<page-path>" | "gh:<source>:<tag-or-repo>"
    source: str      # key from sources.yaml, e.g. "example-creator", "anthropic-claude-code-docs"
    authority: str   # "canonical" | "practice"
    kind: str        # "transcript" | "note" | "doc" | "change" | "release" | "repo"
    date: str        # YYYY-MM-DD
    title: str
    url: str
    path: str        # path relative to project ROOT of the markdown file on disk
    tags: list[str] = field(default_factory=list)
    summary: str = ""
    text: str = ""   # body text (for indexing); may be empty for kind=repo
    meta: dict = field(default_factory=dict)
