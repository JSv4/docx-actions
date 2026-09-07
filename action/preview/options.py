"""Validated presentation settings shared by rendering and GitHub transport."""

from dataclasses import dataclass, fields
import json
import os


@dataclass(frozen=True)
class Options:
    mode: str = 'redline'
    pages: bool = False
    comments: bool = True
    inline_preview: bool = True
    change_log: bool = True
    downloads: bool = True
    summary: bool = True
    context_paragraphs: int = 1
    preview_count: int = 2
    max_passages: int = 0
    comment_budget: int = 58000

    def __post_init__(self):
        if self.mode not in ('redline', 'latest', 'both'):
            raise ValueError('mode must be redline, latest, or both')
        for name in ('pages', 'comments', 'inline_preview', 'change_log', 'downloads', 'summary'):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f'{name.replace("_", "-")} must be a boolean')
        for name, low, high in [('context_paragraphs', 0, 10), ('preview_count', 0, 20),
                                 ('max_passages', 0, 10000), ('comment_budget', 4000, 58000)]:
            value = getattr(self, name)
            if type(value) not in (int, float) or not float(value).is_integer() or not low <= value <= high:
                raise ValueError(f'{name.replace("_", "-")} must be an integer from {low} to {high}')
            object.__setattr__(self, name, int(value))

    @classmethod
    def from_env(cls):
        values = json.loads(os.environ.get('REVIEW_OPTIONS', '{}'))
        return cls(**{f.name: values[f.name.replace('_', '-')] for f in fields(cls)
                      if f.name.replace('_', '-') in values})


if __name__ == '__main__':
    Options.from_env()
