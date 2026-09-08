"""Validated presentation settings shared by rendering and GitHub transport."""

from dataclasses import dataclass, fields
import json
import os
import re


IMAGE_URL = 'https://docx-actions.invalid/preview-images'


@dataclass(frozen=True)
class Options:
    mode: str = 'redline'
    pages: bool = False
    image_host: str = 'auto'
    preview_branch: str = 'docx-previews'
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
        if self.image_host not in ('auto', 'branch', 'none'):
            raise ValueError('image-host must be auto, branch, or none')
        # Limit the ref to a predictable subset of valid Git branch names.
        if (not isinstance(self.preview_branch, str) or len(self.preview_branch) > 200
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]*', self.preview_branch)
                or '..' in self.preview_branch or '@{' in self.preview_branch
                or any(not part or part.startswith('.') or part.endswith(('.', '.lock'))
                       for part in self.preview_branch.split('/'))
                or self.preview_branch == 'HEAD'):
            raise ValueError('preview-branch must be a valid branch name')
        for name in ('pages', 'comments', 'inline_preview', 'change_log', 'downloads', 'summary'):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f'{name.replace("_", "-")} must be a boolean')
        for name, low, high in [('context_paragraphs', 0, 10), ('preview_count', 0, 20),
                                 ('max_passages', 0, 10000), ('comment_budget', 4000, 58000)]:
            value = getattr(self, name)
            if type(value) not in (int, float) or not float(value).is_integer() or not low <= value <= high:
                raise ValueError(f'{name.replace("_", "-")} must be an integer from {low} to {high}')
            object.__setattr__(self, name, int(value))

    @property
    def images_enabled(self):
        return (self.inline_preview and self.preview_count > 0
                and self.image_host != 'none'
                and (self.comments if self.image_host == 'branch' else self.pages))

    @classmethod
    def from_env(cls):
        values = json.loads(os.environ.get('REVIEW_OPTIONS', '{}'))
        return cls(**{f.name: values[f.name.replace('_', '-')] for f in fields(cls)
                      if f.name.replace('_', '-') in values})


if __name__ == '__main__':
    Options.from_env()
