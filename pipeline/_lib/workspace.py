"""Workspace loader. A workspace = one album theme (Suno wid + Jinja2 prompt templates)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from . import paths


@dataclass(frozen=True)
class Workspace:
    name: str
    root: Path
    config: dict

    @property
    def wid(self) -> str:
        return self.config["suno"]["wid"]

    @property
    def default_prompt_variant(self) -> str:
        return self.config.get("default_prompt_variant", "3_2")

    def template_text(self, variant: str) -> str:
        """Return the raw template text (used for hashing). variant e.g. '3_1' or '7'."""
        p = self.root / f"prompt_{variant}.j2"
        return p.read_text(encoding="utf-8")

    def render(self, variant: str, **context) -> str:
        env = Environment(
            loader=FileSystemLoader(str(self.root)),
            undefined=StrictUndefined,
            keep_trailing_newline=True,
        )
        tmpl = env.get_template(f"prompt_{variant}.j2")
        # Expose workspace config to templates so they can reference fields
        # like {{ vocal }}, {{ vocal_style }}, etc. Caller-supplied kwargs
        # override config keys with the same name.
        ctx = dict(self.config)
        ctx.update(context)
        return tmpl.render(**ctx)

    def config_text(self) -> str:
        """Raw config.yaml text — used as a hash input so config changes invalidate caches."""
        return (self.root / "config.yaml").read_text(encoding="utf-8")


def load(name: str) -> Workspace:
    root = paths.WORKSPACES / name
    if not root.is_dir():
        raise FileNotFoundError(f"Workspace not found: {root}")
    cfg = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    return Workspace(name=name, root=root, config=cfg)
