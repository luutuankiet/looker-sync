"""A speedy LookML parser and serializer implemented in pure Python."""

import io
from typing import IO, Optional, Union

from .lexer import Lexer
from .parser import Parser
from .simple import DictParser, DictVisitor
from .tree import DocumentNode


def parse(text: str) -> DocumentNode:
    """Parse LookML into a parse tree.

    Args:
        text: The LookML string to be parsed.

    Returns:
        A document node, the root of the parse tree.

    """
    lexer = Lexer(text)
    tokens = lexer.scan()
    parser = Parser(tokens)
    tree: DocumentNode = parser.parse()
    return tree


def load(stream: Union[str, IO]) -> dict:
    """Parse LookML into a Python dictionary.

    Args:
        stream: File object or string containing LookML to be parsed

    Raises:
        TypeError: If stream is neither a string or a file object

    """

    if isinstance(stream, io.TextIOWrapper):
        text = stream.read()
    elif isinstance(stream, str):
        text = stream
    else:
        raise TypeError("Input stream must be a string or file object.")
    tree: DocumentNode = parse(text)
    visitor = DictVisitor()
    tree_as_dict: dict = visitor.visit(tree)
    return tree_as_dict


def dump(obj: dict, file_object: IO = None) -> Optional[str]:
    """Serialize a Python dictionary into LookML.

    Args:
        obj: The Python dictionary to be serialized to LookML
        file_object: An optional file object to save the LookML string to

    Returns:
        A LookML string if no file_object is passed

    """
    parser = DictParser()
    tree: DocumentNode = parser.parse(obj)
    result = str(tree)
    if file_object:
        file_object.write(result)
        return None
    else:
        return result
