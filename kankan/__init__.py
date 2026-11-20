"""kankan: coordinates AI coding agents on one Git repository through a Trello board."""

from importlib.resources import files

__version__ = files(__name__).joinpath("VERSION").read_text(encoding="ascii").strip()
