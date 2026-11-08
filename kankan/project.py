"""Everything a command needs about one project, bundled."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from kankan import auditlog
from kankan.config import Config, Paths, load_config, load_credentials
from kankan.refstore import RefStore
from kankan.trello import TrelloClient


@dataclass
class Project:
    paths: Paths
    cfg: Config
    refs: RefStore
    trello: TrelloClient

    @classmethod
    def open(cls, cwd) -> "Project":
        paths = Paths.discover(Path(cwd))
        cfg = load_config(paths)
        key, token = load_credentials(paths, cfg)
        trello = TrelloClient(key, token, base_url=cfg.trello_base_url, timeout=cfg.trello_timeout)
        return cls(paths=paths, cfg=cfg, refs=RefStore(paths.repo, cfg.main_branch), trello=trello)

    def reload_config(self) -> Config:
        self.cfg = load_config(self.paths)
        return self.cfg

    def log(self, event: str, /, **fields) -> None:
        auditlog.append(self.paths, event, **fields)
