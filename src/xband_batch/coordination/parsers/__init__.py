"""Parser package: import all built-in parsers so decorators register them."""

from . import legacy_excel, pdf, standard, word
from .base import ListParser, get_parser, register_parser

__all__ = ["ListParser", "get_parser", "register_parser", "legacy_excel", "word", "pdf", "standard"]
