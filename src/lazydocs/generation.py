"""Main module for markdown generation."""

import datetime
import importlib
import importlib.util
import inspect
import os
import pkgutil
import re
import subprocess
import sys
import types
from collections import defaultdict
from enum import Enum
from pathlib import Path
from pydoc import locate
from typing import Any, Callable, Dict, List, Optional

import mdformat


class ClassTypes:
    CLASS = "class"
    ENUM = "enum"
    EXCEPTION = "exception"


_RE_BLOCKSTART_LIST = re.compile(
    r"(Args:|Arg:|Arguments:|Parameters:|Kwargs:|Attributes:|Returns:|Yields:|Kwargs:|Raises:).{0,2}$",
    re.IGNORECASE,
)

_RE_BLOCKSTART_TEXT = re.compile(r"(Examples:|Example:|Todo:).{0,2}$", re.IGNORECASE)

_RE_QUOTE_TEXT = re.compile(r"(Notes:|Note:).{0,2}$", re.IGNORECASE)

_RE_TYPED_ARGSTART = re.compile(r"([\w\[\]_]{1,}?)\s*?\((.*?)\):(.{2,})", re.IGNORECASE)
_RE_ARGSTART = re.compile(r"(\**[\w\[\]_]{1,}?)\s*?:(.{2,})?", re.IGNORECASE)

_IGNORE_GENERATION_INSTRUCTION = "lazydocs: ignore"

# String templates

_SEPARATOR = """
---
"""

_FUNCTIONS_TEMPLATE = """
{section} Functions
{functions}
"""

_METHODS_TEMPLATE = """
{section} Methods
{functions}
"""

_FUNC_TEMPLATE = """
{section} `{header}`

{doc}
{source}
```python
{funcdef}
```
"""

_EXCEPTIONS_TEMPLATE = """
{section} Exceptions
{classes}
"""

_ENUMS_TEMPLATE = """
{section} Enums
{classes}
"""

_CLASS_TEMPLATE = """
{section} {kind} `{header}`
{doc}
{init}
{variables}
{handlers}
{methods}
"""

_VARIABLES_TEMPLATE = """
{section} Variables
{variables}
"""

_MODULE_TEMPLATE = """
{section} module `{header}`
{doc}
{global_vars}
{functions}
{classes}
{exceptions}
{enums}
"""

_OVERVIEW_TEMPLATE = """
# API Overview

## Modules
{modules}

## Classes
{classes}

## Functions
{functions}
"""

_WATERMARK_TEMPLATE = """

---

_This file was automatically generated via [lazydocs](https://github.com/ml-tooling/lazydocs)._
"""

_MKDOCS_PAGES_TEMPLATE = """title: API Reference
nav:
    - Overview: {overview_file}
    - ...
"""


def _to_source_link(path: str) -> str:
    return "\n [**Link to source**](%s)\n" % path


def _get_function_signature(
    function: Callable,
    owner_class: Any = None,
    show_module: bool = False,
    ignore_self: bool = False,
    wrap_arguments: bool = False,
    remove_package: bool = False,
) -> str:
    """Generates a string for a function signature.

    Args:
        function: Selected function (or method) to generate the signature string.
        owner_class: Owner class of this function.
        show_module: If `True`, add module path in function signature.
        ignore_self: If `True`, ignore self argument in function signature.
        wrap_arguments:  If `True`, wrap all arguments to new lines.
        remove_package:  If `True`, the package path will be removed from the function signature.

    Returns:
        str: Signature of selected function.
    """
    isclass = inspect.isclass(function)

    # Get base name.
    name_parts = []
    if show_module:
        name_parts.append(function.__module__)
    if owner_class:
        name_parts.append(owner_class.__name__)
    if hasattr(function, "__name__"):
        name_parts.append(function.__name__)
    else:
        name_parts.append(type(function).__name__)
        name_parts.append("__call__")

        function = function.__call__  # type: ignore
    name = ".".join(name_parts)

    if isclass:
        function = getattr(function, "__init__", None)

    arguments = []
    return_type = ""
    if hasattr(inspect, "signature"):
        parameters = inspect.signature(function).parameters
        if inspect.signature(function).return_annotation != inspect.Signature.empty:
            return_type = str(inspect.signature(function).return_annotation)
            if return_type.startswith("<class"):
                # Base class -> get real name
                try:
                    return_type = inspect.signature(function).return_annotation.__name__
                except Exception:
                    pass
            # Remove all typing path prefixes
            return_type = return_type.replace("typing.", "")
            if remove_package:
                # Remove all package path return type
                return_type = re.sub(r"([a-zA-Z0-9_]*?\.)", "", return_type)

        for parameter in parameters:
            argument = str(parameters[parameter])
            if ignore_self and argument == "self":
                # Ignore self
                continue
            # Reintroduce Optionals
            argument = re.sub(r"Union\[(.*?), NoneType\]", r"Optional[\1]", argument)

            # Remove package
            if remove_package:
                # Remove all package path from parameter signature
                if "=" not in argument:
                    argument = re.sub(r"([a-zA-Z0-9_]*?\.)", "", argument)
                else:
                    # Remove only from part before the first =
                    argument_split = argument.split("=")
                    argument_split[0] = re.sub(
                        r"([a-zA-Z0-9_]*?\.)", "", argument_split[0]
                    )
                    argument = "=".join(argument_split)
            arguments.append(argument)

    signature = name + "("
    if wrap_arguments:
        for i, arg in enumerate(arguments):
            signature += "\n    " + arg

            signature += "," if i is not len(arguments) - 1 else "\n"
    else:
        signature += ", ".join(arguments)

    signature += ")" + ((" → " + return_type) if return_type else "")

    return signature


def _order_by_index(objs: Any, index: List[int]) -> List[str]:
    """Orders the set of `objs` by `line_nos`."""
    ordering = sorted(range(len(index)), key=index.__getitem__)
    return [objs[i] for i in ordering]


def to_md_file(
    markdown_str: str,
    filename: str,
    out_path: str = ".",
    watermark: bool = True,
    disable_markdownlint: bool = True,
    pretty: bool = True,
) -> None:
    """Creates an API docs file from a provided text.

    Args:
        markdown_str (str): Markdown string with line breaks to write to file.
        filename (str): Filename without the .md
        watermark (bool): If `True`, add a watermark with a timestamp to bottom of the markdown files.
        disable_markdownlint (bool): If `True`, an inline tag is added to disable markdownlint for this file.
        out_path (str): The output directory
    """
    if not markdown_str:
        # Dont write empty files
        return

    md_file = filename
    if not filename.endswith(".md"):
        md_file = filename + ".md"

    if disable_markdownlint:
        markdown_str = "<!-- markdownlint-disable -->\n" + markdown_str

    if watermark:
        markdown_str += _WATERMARK_TEMPLATE.format(
            date=datetime.date.today().strftime("%d %b %Y")
        )

    if pretty:
        markdown_str = mdformat.text(markdown_str)

    print("Writing {}.".format(md_file))
    with open(os.path.join(out_path, md_file), "w", encoding="utf-8") as f:
        f.write(markdown_str)


def _get_line_no(obj: Any) -> Optional[int]:
    """Gets the source line number of this object. None if `obj` code cannot be found."""
    try:
        return inspect.getsourcelines(obj)[1]
    except Exception:
        # no code found
        return None


def _get_class_that_defined_method(meth: Any) -> Any:
    if inspect.ismethod(meth):
        for cls in inspect.getmro(meth.__self__.__class__):
            if cls.__dict__.get(meth.__name__) is meth:
                return cls
        meth = meth.__func__  # fallback to __qualname__ parsing
    if inspect.isfunction(meth):
        mod = inspect.getmodule(meth)
        if mod is None:
            return None
        method_name = meth.__qualname__.split(".<locals>", 1)[0].rsplit(".", 1)[0]
        # If the method was defined elsewhere (i.e.: dataclasses), it'll not
        # be available in the mod.
        cls = getattr(mod, method_name, None)
        if isinstance(cls, type):
            return cls
    return getattr(meth, "__objclass__", None)  # handle special descriptor objects


def _get_docstring(obj: Any) -> str:
    return "" if obj.__doc__ is None else inspect.getdoc(obj) or ""


def _is_object_ignored(obj: Any) -> bool:
    if (
        _IGNORE_GENERATION_INSTRUCTION.replace(" ", "").lower()
        in _get_docstring(obj).replace(" ", "").lower()
    ):
        # Do not generate anything if docstring contains ignore instruction
        return True
    return False


def _is_module_ignored(module_name: str, ignored_modules: List[str]) -> bool:
    """Checks if a given module is ignored."""
    if module_name.split(".")[-1].startswith("_"):
        return True

    for ignored_module in ignored_modules:
        if module_name == ignored_module:
            return True

        # Check is module is subpackage of an ignored package
        if module_name.startswith(ignored_module + "."):
            return True

    return False


def _get_doc_summary(obj: Any) -> str:
    # First line should contain the summary
    return _get_docstring(obj).split("\n")[0]


def _get_anchor_tag(header: str) -> str:
    anchor_tag = header.strip().lower()
    # Whitespaces to -
    anchor_tag = re.compile(r"\s").sub("-", anchor_tag)
    # Remove not allowed characters
    anchor_tag = re.compile(r"[^a-zA-Z0-9-_]").sub("", anchor_tag)
    return anchor_tag


def _doc2md(obj: Any) -> str:
    """Parse docstring (with getdoc) according to Google-style formatting and convert to markdown.

    Args:
        obj: Selected object for markdown generation.

    Returns:
        str: Markdown documentation for docstring of selected object.
    """
    # TODO Evaluate to use: https://github.com/rr-/docstring_parser
    # The specfication of Inspect#getdoc() was changed since version 3.5,
    # the documentation strings are now inherited if not overridden.
    # For details see: https://docs.python.org/3.6/library/inspect.html#inspect.getdoc
    # doc = getdoc(func) or ""
    doc = _get_docstring(obj)

    blockindent = 0
    argindent = 1
    out = []
    arg_list = False
    literal_block = False
    md_code_snippet = False
    quote_block = False
    snippet_indent = 0

    for line in doc.split("\n"):
        indent = len(line) - len(line.lstrip())
        if not md_code_snippet and not literal_block:
            line = line.lstrip()
        else:
            line = line[snippet_indent:]

        if line.startswith(">>>"):
            # support for doctest
            line = line.replace(">>>", "```") + "```"

        if (
            _RE_BLOCKSTART_LIST.match(line)
            or _RE_BLOCKSTART_TEXT.match(line)
            or _RE_QUOTE_TEXT.match(line)
        ):
            blockindent = indent

            if quote_block:
                quote_block = False

            if literal_block:
                # break literal block
                out.append("```\n")
                literal_block = False

            out.append("\n\n**{}**\n".format(line.strip()))

            arg_list = bool(_RE_BLOCKSTART_LIST.match(line))

            if _RE_QUOTE_TEXT.match(line):
                quote_block = True
                out.append("\n>")
        elif line.strip().startswith("```"):
            # Code snippet is used
            if md_code_snippet:
                md_code_snippet = False
                snippet_indent = 0
            else:
                md_code_snippet = True
                snippet_indent = indent
                out.append("\n")

            out.append(line)
        elif line.strip().endswith("::"):
            # Literal Block Support: https://docutils.sourceforge.io/docs/user/rst/quickref.html#literal-blocks
            literal_block = True
            out.append(line.replace("::", ":\n```"))
        elif quote_block:
            out.append(line.strip() + " ")
        elif line.strip().startswith("-"):
            # Allow bullet lists
            out.append("\n" + (" " * indent) + line)
        elif indent > blockindent:
            if arg_list and not literal_block and _RE_TYPED_ARGSTART.match(line):
                # start of new argument
                out.append(
                    "\n"
                    + " " * blockindent
                    + " - "
                    + _RE_TYPED_ARGSTART.sub(r"<b>`\1`</b> (\2): \3", line)
                )
                argindent = indent
            elif arg_list and not literal_block and _RE_ARGSTART.match(line):
                # start of an exception-type block
                out.append(
                    "\n"
                    + " " * blockindent
                    + " - "
                    + _RE_ARGSTART.sub(r"<b>`\1`</b>: \2", line)
                )
                argindent = indent
            elif arg_list and indent > argindent:
                # attach docs text of argument
                # * (blockindent + 2)
                out.append(" " + line)
            else:
                out.append(line)
        else:
            if line.strip() and literal_block:
                # indent has changed, if not empty line, break literal block
                line = "```\n" + line
                literal_block = False
            if not literal_block:
                line += " "
            out.append(line)

        if md_code_snippet:
            out.append("\n")
        elif not line.strip() and not quote_block:
            out.append("\n\n")
        elif not line and quote_block:
            out.append("\n>")

    # Remove trailing whitespace from lines
    content = "\n".join(line.rstrip() for line in "".join(out).splitlines())

    return content


class MarkdownGenerator(object):
    """Markdown generator class."""

    def __init__(
        self,
        src_root_path: Optional[str] = None,
        src_base_url: Optional[str] = None,
        remove_package_prefix: bool = False,
    ):
        """Initializes the markdown API generator.

        Args:
            src_root_path: The root folder name containing all the sources.
            src_base_url: The base github link. Should include branch name.
                All source links are generated with this prefix.
            remove_package_prefix: If `True`, the package prefix will be removed from all functions and methods.
        """
        self.src_root_path = src_root_path
        self.src_base_url = src_base_url
        self.remove_package_prefix = remove_package_prefix

        self.generated_objects: List[Dict] = []

    def _get_class_type(self, cls: Any) -> str:
        if issubclass(cls, Enum):
            kind = ClassTypes.ENUM
        elif issubclass(cls, Exception):
            kind = ClassTypes.EXCEPTION
        else:
            kind = ClassTypes.CLASS

        return kind

    def _get_src_path(self, obj: Any, append_base: bool = True) -> str:
        """Creates a src path string with line info for use as markdown link.

        Args:
            obj (Any): Selected object to get the src path.
            append_base (bool, optional): If `True`, the src repo url will be appended. Defaults to True.

        Returns:
            str: Source code path with line marker.
        """
        src_root_path = None
        if self.src_root_path:
            src_root_path = os.path.abspath(self.src_root_path)
        else:
            return ""

        try:
            src_file = inspect.getsourcefile(obj)
            if not src_file:
                return ""
            if src_file == "<string>":
                return ""

            path = os.path.abspath(src_file)
        except Exception:
            return ""

        assert isinstance(path, str)

        if src_root_path not in path:
            # this can happen with e.g.
            # inlinefunc-wrapped functions
            if hasattr(obj, "__module__"):
                path = "%s.%s" % (obj.__module__, obj.__name__)
            else:
                path = obj.__name__

            assert isinstance(path, str)
            path = path.replace(".", "/")

        src_path = Path(os.path.abspath(path)).relative_to(src_root_path).as_posix()
        if append_base and self.src_base_url:
            base = self.src_base_url
            if base.endswith("/"):
                base = base[:-1]
            if src_path.startswith("/"):
                src_path = src_path[1:]
            src_path = f"{base}/{src_path}"

        lineno = _get_line_no(obj)
        anchor = "" if lineno is None else "#L{}".format(lineno)
        return src_path + anchor

    def func2md(self, func: Callable, clsname: str = "", depth: int = 3) -> str:
        """Takes a function (or method) and generates markdown docs.

        Args:
            func (Callable): Selected function (or method) for markdown generation.
            clsname (str, optional): Class name to prepend to funcname. Defaults to "".
            depth (int, optional): Number of # to append to class name. Defaults to 3.

        Returns:
            str: Markdown documentation for selected function.
        """
        if _is_object_ignored(func):
            # The function is ignored from generation
            return ""

        section = "#" * depth
        funcname = func.__name__
        modname = None
        if hasattr(func, "__module__"):
            modname = func.__module__

        escfuncname = (
            "%s" % funcname if funcname.startswith("_") else funcname
        )  # "`%s`"

        full_name = "%s%s" % ("%s." % clsname if clsname else "", escfuncname)
        header = full_name

        if self.remove_package_prefix:
            # TODO: Evaluate
            # Only use the name
            header = escfuncname

        path = self._get_src_path(func)
        doc = _doc2md(func)
        summary = _get_doc_summary(func)

        funcdef = _get_function_signature(
            func, ignore_self=True, remove_package=self.remove_package_prefix
        )

        # split the function definition if it is too long
        lmax = 80
        if len(funcdef) > lmax:
            funcdef = _get_function_signature(
                func,
                ignore_self=True,
                wrap_arguments=True,
                remove_package=self.remove_package_prefix,
            )

        if inspect.ismethod(func):
            func_type = "classmethod"
        else:
            if _get_class_that_defined_method(func) is None:
                func_type = "function"
            else:
                # function of a class
                func_type = "method"

        self.generated_objects.append(
            {
                "type": func_type,
                "name": header,
                "full_name": full_name,
                "module": modname,
                "anchor_tag": _get_anchor_tag(func_type + "-" + header),
                "description": summary,
            }
        )

        # build the signature
        markdown = _FUNC_TEMPLATE.format(
            section=section,
            header=header,
            source=_to_source_link(path) if path else "",
            funcdef=funcdef,
            func_type=func_type,
            doc=doc if doc else "*No documentation found.*",
        )

        return markdown

    def class2md(self, cls: Any, depth: int = 1) -> str:
        """Takes a class and creates markdown text to document its methods and variables.

        Args:
            cls (class): Selected class for markdown generation.
            depth (int, optional): Number of # to append to function name. Defaults to 1.

        Returns:
            str: Markdown documentation for selected class.
        """
        if _is_object_ignored(cls):
            # The class is ignored from generation
            return ""

        section = "#" * depth
        subsection = "#" * (depth + 1)
        clsname = cls.__name__
        modname = cls.__module__
        header = clsname
        path = self._get_src_path(cls)
        doc = _doc2md(cls)
        summary = _get_doc_summary(cls)

        self.generated_objects.append(
            {
                "type": "class",
                "name": header,
                "full_name": header,
                "module": modname,
                "anchor_tag": _get_anchor_tag("class-" + header),
                "description": summary,
            }
        )

        try:
            # object module should be the same as the calling module
            if (
                hasattr(cls.__init__, "__module__")
                and cls.__init__.__module__ == modname
            ):
                init = self.func2md(cls.__init__, clsname=clsname)
            else:
                init = ""
        except (ValueError, TypeError):
            # this happens if __init__ is outside the repo
            init = ""

        variables = []
        properties = []

        # Handle different kinds of classes
        kind = self._get_class_type(cls)
        if kind == ClassTypes.ENUM:
            sectionheader = "#" * (depth + 1)
            values = "\n".join("- **%s** = %s" % (obj.name, obj.value) for obj in cls)
            variables.append("%s Values\n%s" % (sectionheader, values))

        for name, obj in inspect.getmembers(
            cls, lambda a: not (inspect.isroutine(a) or inspect.ismethod(a))
        ):
            if not name.startswith("_") and type(obj) == property:
                comments = _doc2md(obj) or inspect.getcomments(obj)
                comments = "\n\n%s" % comments if comments else ""
                property_name = f"{clsname}.{name}"
                if self.remove_package_prefix:
                    property_name = name
                properties.append("- `%s`%s\n" % (property_name, comments))
        if properties:
            variables.append("%s Properties\n%s" % (subsection, "\n".join(properties)))

        handlers = []
        for name, obj in inspect.getmembers(cls, inspect.ismethoddescriptor):
            if (
                not name.startswith("_")
                and hasattr(obj, "__module__")
                # object module should be the same as the calling module
                and obj.__module__ == modname
            ):
                handler_name = f"{clsname}.{name}"
                if self.remove_package_prefix:
                    handler_name = name

                handlers.append(
                    _SEPARATOR + "\n%s handler %s\n" % (subsection, handler_name)
                )

        methods = []
        # for name, obj in getmembers(cls, inspect.isfunction):
        for name, obj in inspect.getmembers(
            cls, lambda a: inspect.ismethod(a) or inspect.isfunction(a)
        ):
            if (
                not name.startswith("_")
                and hasattr(obj, "__module__")
                and name not in handlers
                # object module should be the same as the calling module
                and obj.__module__ == modname
            ):
                function_md = self.func2md(obj, clsname=clsname, depth=depth + 2)
                if function_md:
                    methods.append(_SEPARATOR + function_md)

        methods_md = (
            _METHODS_TEMPLATE.format(
                section="#" * (depth + 1),
                functions="".join(methods),
            )
            if methods
            else ""
        )

        markdown = _CLASS_TEMPLATE.format(
            kind=ClassTypes.CLASS.capitalize() if kind == ClassTypes.CLASS else "",
            section=section,
            header=header,
            source=_to_source_link(path) if path else "",
            doc=doc if doc else "",
            init=init,
            variables="".join(variables),
            handlers="".join(handlers),
            methods=methods_md,
        )

        return markdown

    def module2md(self, module: types.ModuleType, depth: int = 1) -> str:
        """Takes an imported module object and create a Markdown string containing functions and classes.

        Args:
            module (types.ModuleType): Selected module for markdown generation.
            depth (int, optional): Number of # to append before module heading. Defaults to 1.

        Returns:
            str: Markdown documentation for selected module.
        """
        if _is_object_ignored(module):
            # The module is ignored from generation
            return ""

        modname = module.__name__
        doc = _doc2md(module)
        summary = _get_doc_summary(module)
        path = self._get_src_path(module)
        exported = getattr(module, "__all__", None)
        found = []

        self.generated_objects.append(
            {
                "type": "module",
                "name": modname,
                "full_name": modname,
                "module": modname,
                "anchor_tag": _get_anchor_tag("module-" + modname),
                "description": summary,
            }
        )

        classes: Dict[str, list] = defaultdict(list)
        for name, obj in inspect.getmembers(module, inspect.isclass):
            found.append(name)
            if name.startswith("_"):
                continue
            if exported is not None and name not in exported:
                continue
            if (
                exported is None
                and hasattr(obj, "__module__")
                and obj.__module__ != modname
            ):
                continue

            kind = self._get_class_type(obj)
            class_markdown = self.class2md(
                obj, depth=depth if kind == ClassTypes.CLASS else depth + 1
            )
            if class_markdown:
                classes[kind].append(_SEPARATOR + class_markdown)

        exceptions_md = (
            _EXCEPTIONS_TEMPLATE.format(
                section="#" * depth,
                classes="".join([c for c in classes[ClassTypes.EXCEPTION]]),
            )
            if classes[ClassTypes.EXCEPTION]
            else ""
        )

        enums_md = (
            _ENUMS_TEMPLATE.format(
                section="#" * depth,
                classes="".join([c for c in classes[ClassTypes.ENUM]]),
            )
            if classes[ClassTypes.ENUM]
            else ""
        )

        functions: List[str] = []
        index = []
        for name, obj in inspect.getmembers(module, inspect.isfunction):
            found.append(name)
            if name.startswith("_"):
                continue
            if exported is not None and name not in exported:
                continue
            if (
                exported is None
                and hasattr(obj, "__module__")
                and obj.__module__ != modname
            ):
                continue

            function_md = self.func2md(obj, depth=depth + 1)
            if function_md:
                functions.append(_SEPARATOR + function_md)
                if exported is None:
                    index.append(_get_line_no(obj) or 0)
                else:
                    index.append(exported.index(name))

        functions = _order_by_index(functions, index)
        functions_md = _FUNCTIONS_TEMPLATE.format(
            section="#" * depth, functions="\n".join(functions)
        )

        variables: List[str] = []
        index = []
        for name, obj in module.__dict__.items():
            if name.startswith("_") or name in found:
                continue
            if exported is not None and name not in exported:
                continue
            if exported is None:
                if hasattr(obj, "__module__") and obj.__module__ != modname:
                    continue
                if hasattr(obj, "__name__") and not obj.__name__.startswith(modname):
                    continue

            comments = inspect.getcomments(obj)
            comments = ": %s" % comments if comments else ""
            variables.append("- **%s**%s" % (name, comments))

            if exported is None:
                index.append(_get_line_no(obj) or 0)
            else:
                index.append(exported.index(name))

        variables_section = ""
        variables = _order_by_index(variables, index)
        if variables:
            variables_section = _VARIABLES_TEMPLATE.format(
                section="#" * depth,
                variables="\n".join(variables),
            )

        markdown = _MODULE_TEMPLATE.format(
            section="#" * depth,
            header=modname,
            source=_to_source_link(path) if path else "",
            doc=doc,
            global_vars=variables_section,
            functions=functions_md if functions_md else "",
            classes="".join([c for c in classes[ClassTypes.CLASS]]),
            exceptions=exceptions_md,
            enums=enums_md,
        )

        return markdown

    def import2md(self, obj: Any, depth: int = 1) -> str:
        """Generates markdown documentation for a selected object/import.

        Args:
            obj (Any): Selcted object for markdown docs generation.
            depth (int, optional): Number of # to append before heading. Defaults to 1.

        Returns:
            str: Markdown documentation of selected object.
        """
        if inspect.isclass(obj):
            return self.class2md(obj, depth=depth)
        elif isinstance(obj, types.ModuleType):
            return self.module2md(obj, depth=depth)
        elif callable(obj):
            return self.func2md(obj, depth=depth)
        else:
            print(f"Could not generate markdown for object type {str(type(obj))}")
            return ""

    def overview2md(self) -> str:
        """Generates a documentation overview file based on the generated docs."""

        entries_md = ""
        for obj in list(
            filter(lambda d: d["type"] == "module", self.generated_objects)
        ):
            full_name = obj["full_name"]
            if "module" in obj:
                link = "./" + obj["module"] + ".md#" + obj["anchor_tag"]
            else:
                link = "#unknown"

            description = obj["description"]
            entries_md += f"\n- [`{full_name}`]({link})"
            if description:
                entries_md += ": " + description
        if not entries_md:
            entries_md = "\n- No modules"
        modules_md = entries_md

        entries_md = ""
        for obj in list(filter(lambda d: d["type"] == "class", self.generated_objects)):
            module_name = obj["module"].split(".")[-1]
            name = module_name + "." + obj["full_name"]
            link = "./" + obj["module"] + ".md#" + obj["anchor_tag"]
            description = obj["description"]
            entries_md += f"\n- [`{name}`]({link})"
            if description:
                entries_md += ": " + description
        if not entries_md:
            entries_md = "\n- No classes"
        classes_md = entries_md

        entries_md = ""
        for obj in list(
            filter(lambda d: d["type"] == "function", self.generated_objects)
        ):
            module_name = obj["module"].split(".")[-1]
            name = module_name + "." + obj["full_name"]
            link = "./" + obj["module"] + ".md#" + obj["anchor_tag"]
            description = obj["description"]
            entries_md += f"\n- [`{name}`]({link})"
            if description:
                entries_md += ": " + description
        if not entries_md:
            entries_md = "\n- No functions"
        functions_md = entries_md

        return _OVERVIEW_TEMPLATE.format(
            modules=modules_md, classes=classes_md, functions=functions_md
        )


def generate_docs(
    paths: List[str],
    output_path: str = "./docs",
    src_root_path: Optional[str] = None,
    src_base_url: Optional[str] = None,
    remove_package_prefix: bool = False,
    ignored_modules: Optional[List[str]] = None,
    overview_file: Optional[str] = None,
    watermark: bool = True,
    validate: bool = False,
) -> None:
    """Generates markdown documentation for provided paths based on Google-style docstrings.

    Args:
        paths: Selected paths or import name for markdown generation.
        output_path: The output path for the creation of the markdown files. Set this to `stdout` to print all markdown to stdout.
        src_root_path: The root folder name containing all the sources. Fallback to git repo root.
        src_base_url: The base url of the github link. Should include branch name. All source links are generated with this prefix.
        remove_package_prefix: If `True`, the package prefix will be removed from all functions and methods.
        ignored_modules: A list of modules that should be ignored.
        overview_file: Filename of overview file. If not provided, no overview file will be generated.
        watermark: If `True`, add a watermark with a timestamp to bottom of the markdown files.
        validate: If `True`, validate the docstrings via pydocstyle. Requires pydocstyle to be installed.
    """
    stdout_mode = output_path.lower() == "stdout"

    if not stdout_mode and not os.path.exists(output_path):
        # Create output path
        os.makedirs(output_path)

    if not ignored_modules:
        ignored_modules = list()

    if not src_root_path:
        try:
            # Set src root path to git root
            src_root_path = (
                subprocess.Popen(
                    ["git", "rev-parse", "--show-toplevel"], stdout=subprocess.PIPE
                )
                .communicate()[0]
                .rstrip()
                .decode("utf-8")
            )
            if src_root_path and src_base_url is None and not stdout_mode:
                # Set base url to be relative to the git root folder based on output_path
                src_base_url = os.path.relpath(
                    src_root_path, os.path.abspath(output_path)
                )
                if sys.platform == "win32":
                    src_base_url = src_base_url.replace("\\", "/")
        except Exception:
            # Ignore all exceptions
            pass

    # Initialize Markdown Generator
    generator = MarkdownGenerator(
        src_root_path=src_root_path,
        src_base_url=src_base_url,
        remove_package_prefix=remove_package_prefix,
    )
    pydocstyle_cmd = 'pydocstyle --match="^(?!_(?!_))(?!test_).*\.py" --convention=google --add-ignore=D100,D101,D102,D103,D104,D105,D107,D202'

    for path in paths:  # lgtm [py/non-iterable-in-for-loop]
        if validate:
            obj_path = path if os.path.isdir(path) or os.path.isdir(path) else locate(path).__file__
            call_return = subprocess.call(f"{pydocstyle_cmd} {obj_path}", shell=True)
            if call_return > 0:
                raise Exception(f"Validation for {path} failed.")
            else:
                print(f"Validation for {path} passed.")
                continue

        if os.path.isdir(path):
            if not stdout_mode:
                print(f"Generating docs for python package at: {path}")

            # Generate one file for every discovered module
            for loader, module_name, _ in pkgutil.walk_packages([path]):
                if _is_module_ignored(module_name, ignored_modules):
                    # Add module to ignore list, so submodule will also be ignored
                    if not stdout_mode:
                        print(f"Ignoring module: {module_name}")
                    ignored_modules.append(module_name)
                    continue

                try:
                    mod = loader.find_module(module_name).load_module(module_name)  # type: ignore
                    module_md = generator.module2md(mod)
                    if not module_md:
                        # Module md is empty -> ignore module and all submodules
                        # Add module to ignore list, so submodule will also be ignored
                        ignored_modules.append(module_name)
                        continue

                    if stdout_mode:
                        print(module_md)
                    else:
                        to_md_file(
                            module_md,
                            mod.__name__,
                            out_path=output_path,
                            watermark=watermark,
                        )
                except Exception as ex:
                    print(
                        f"Failed to generate docs for module {module_name}: " + repr(ex)
                    )
        elif os.path.isfile(path):
            if not stdout_mode:
                print(f"Generating docs for python module at: {path}")

            module_name = os.path.basename(path)

            spec = importlib.util.spec_from_file_location(
                module_name,
                path,
            )
            assert spec is not None
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore

            if mod:
                module_md = generator.module2md(mod)
                if stdout_mode:
                    print(module_md)
                else:
                    to_md_file(
                        module_md,
                        module_name,
                        out_path=output_path,
                        watermark=watermark,
                    )
            else:
                raise Exception(f"Failed to generate markdown for {path!r}.")
        else:
            # Path seems to be an import
            obj = locate(path)
            if obj is not None:
                # TODO: function to get path to file whatever the object is
                # if validate:
                #     subprocess.call(
                #         f"pydocstyle --convention=google {obj.__file__}", shell=True
                #     )
                if not stdout_mode:
                    print(f"Generating docs for python import: {path}")

                if hasattr(obj, "__path__"):
                    # Object is a package
                    import_md = generator.import2md(obj)
                    if stdout_mode:
                        print(import_md)
                    else:
                        to_md_file(
                            import_md, path, out_path=output_path, watermark=watermark
                        )

                    for loader, module_name, _ in pkgutil.walk_packages(
                        path=obj.__path__,  # type: ignore
                        prefix=obj.__name__ + ".",  # type: ignore
                    ):
                        if _is_module_ignored(module_name, ignored_modules):
                            # Add module to ignore list, so submodule will also be ignored
                            ignored_modules.append(module_name)
                            continue

                        try:
                            mod = loader.find_module(module_name).load_module(  # type: ignore
                                module_name
                            )
                            module_md = generator.module2md(mod)

                            if not module_md:
                                # Module MD is empty -> ignore module and all submodules
                                # Add module to ignore list, so submodule will also be ignored
                                ignored_modules.append(module_name)
                                continue

                            if stdout_mode:
                                print(module_md)
                            else:
                                to_md_file(
                                    module_md,
                                    mod.__name__,
                                    out_path=output_path,
                                    watermark=watermark,
                                )
                        except Exception as ex:
                            print(
                                f"Failed to generate docs for module {module_name}: "
                                + repr(ex)
                            )
                else:
                    import_md = generator.import2md(obj)
                    if stdout_mode:
                        print(import_md)
                    else:
                        to_md_file(
                            import_md, path, out_path=output_path, watermark=watermark
                        )
            else:
                raise Exception(f"Failed to generate markdown for {path!r}.")

    if validate:
        return

    if overview_file and not stdout_mode:
        if not overview_file.endswith(".md"):
            overview_file = overview_file + ".md"

        to_md_file(
            generator.overview2md(),
            overview_file,
            out_path=output_path,
            watermark=watermark,
        )

        # Write mkdocs pages file
        print("Writing mkdocs .pages file.")
        # TODO: generate navigation items to fix problem with naming
        with open(os.path.join(output_path, ".pages"), "w") as f:
            f.write(_MKDOCS_PAGES_TEMPLATE.format(overview_file=overview_file))
