# Copyright (c) 2026 Kevin PANCHOUT
# SPDX-License-Identifier: MIT

"""Naming Check: audit Wwise object names against the project's naming convention.

A compliant name is ``<project>_<type>_<Category>[_anything]``,
the convention being read from the project's ``script/naming_convention.ini``.
"""

import argparse
import configparser
import contextlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from waapi import CannotConnectToWaapiException, WaapiClient, WaapiRequestFailed

CONVENTION_FILE_NAME = "naming_convention.ini"
_SCRIPT_DIR_NAME = "script"
_PLACEHOLDER = "CHANGE_ME"
_CONVENTION_TEMPLATE = f"""\
# Naming convention of this Wwise project.
# A name is: <project>_<type>_<Category>[_anything]
# Example:   ADN_SFX_Laser_PassBy_04
# Category is a free word in CamelCase (Laser, RatsScream); the rest is free.
# Replace every {_PLACEHOLDER} below, save, then run Naming Check again.

# Project code at the start of every name, letters and digits only. Example: ADN
project = {_PLACEHOLDER}

# Allowed sound types, comma-separated, letters and digits only. Example: SFX, AMB, MUS
types = {_PLACEHOLDER}

# Type of Music Tracks, one of the types above. Example: MUS
music_type = {_PLACEHOLDER}
"""
# The file has no section header to stay simple; configparser needs one.
_INI_SECTION = "convention"
_CODE = re.compile(r"[A-Za-z0-9]+")
_DEFAULT_WAAPI_URL = "ws://127.0.0.1:8080/waapi"
_CHECKED_OBJECTS_QUERY = "$ from type Sound, MusicTrack where !IsVoice"
_MUSIC_TRACK = "MusicTrack"
_CAMEL_CASE_WORD = re.compile(r"[A-Z][A-Za-z0-9]*")
_NAME_SEPARATOR = "_"
# project, type, category; anything after is free.
_REQUIRED_PARTS = 3
# Typed instead of "_" by mistake; fixed into "_" when unambiguous.
_WRONG_SEPARATORS = re.compile(r"[ -]+")
_UNDO_GROUP_NAME = "Naming Check fixes"
_EXIT_OK = 0
_EXIT_NON_COMPLIANT = 1
_EXIT_ERROR = 2


class ConventionError(Exception):
    """The convention file is missing, incomplete or invalid.

    The message tells the user which file to edit and what to change.
    """


class RequestRejected(Exception):
    """Wwise rejected a request sent by a client without ``allow_exception``.

    Such a client returns ``None`` instead of raising, so Wwise's reason is lost.
    """


@dataclass(frozen=True)
class Convention:
    """Naming convention of one Wwise project.

    Attributes:
        project: Code every name starts with, e.g. ``ADN``.
        types: Allowed sound types, e.g. ``("SFX", "AMB", "MUS")``.
        music_type: Type given to Music Tracks missing their prefix; one of
            ``types``.

    Raises:
        ValueError: If a code is not made of letters and digits only, ``types``
            is empty or repeats a type (case ignored), or ``music_type`` is not
            one of ``types``. The message names the setting.
    """

    project: str
    types: tuple[str, ...]
    music_type: str

    def __post_init__(self) -> None:
        if not _CODE.fullmatch(self.project):
            raise ValueError(f"project '{self.project}' must be letters and digits")
        listed = ", ".join(self.types)
        if not self.types or not all(_CODE.fullmatch(t) for t in self.types):
            raise ValueError(f"types '{listed}' must be codes of letters and digits")
        if len({t.upper() for t in self.types}) != len(self.types):
            raise ValueError(f"types '{listed}' must not repeat a type")
        if self.music_type not in self.types:
            raise ValueError(f"music_type must be one of the types ({listed})")


@dataclass(frozen=True)
class Issue:
    """A name that does not follow the convention.

    Attributes:
        object_id: Wwise GUID of the object.
        object_type: Wwise type, ``Sound`` or ``MusicTrack``.
        name: Current name.
        reason: First rule the name breaks, for the user to fix it.
        fix: Compliant name deduced with certainty, or ``None`` when the
            name must be fixed by hand.
    """

    object_id: str
    object_type: str
    name: str
    reason: str
    fix: str | None


@dataclass(frozen=True)
class Failure:
    """A fix that Wwise rejected.

    Attributes:
        issue: The issue whose fix was attempted.
        reason: Message returned by Wwise.
    """

    issue: Issue
    reason: str


@dataclass(frozen=True)
class Summary:
    """Result of an audit, and of the fixes when they were applied.

    Attributes:
        analyzed: Number of objects checked.
        issues: Objects whose name does not follow the convention.
        applied: Whether the certain fixes were sent to Wwise.
        failures: Fixes that Wwise rejected; empty in an audit.
    """

    analyzed: int
    issues: tuple[Issue, ...]
    applied: bool = False
    failures: tuple[Failure, ...] = ()

    @property
    def compliant(self) -> int:
        """Number of checked objects whose name followed the convention."""
        return self.analyzed - len(self.issues)

    @property
    def fixable(self) -> tuple[Issue, ...]:
        """Issues whose fix was deduced with certainty."""
        return tuple(issue for issue in self.issues if issue.fix)

    @property
    def renamed(self) -> int:
        """Number of objects actually renamed; 0 in an audit."""
        return len(self.fixable) - len(self.failures) if self.applied else 0

    @property
    def remaining(self) -> int:
        """Number of non-compliant names left in the project."""
        return len(self.issues) - self.renamed


class _Word(NamedTuple):
    text: str
    # Index of the "_" block the word came from, to spot ambiguous splits.
    block: int


def load_convention(script_dir: Path) -> Convention:
    """Read the convention file of a project, creating it on first use.

    Args:
        script_dir: Folder of the project holding the convention file; created
            if missing.

    Returns:
        The convention.

    Raises:
        ConventionError: If the file did not exist (it is then created from a
            template to fill in), cannot be read as ``setting = value`` lines,
            or a setting is missing, repeated, empty, still holds its example
            value or is invalid. The message names the file and the setting.
    """
    path = script_dir / CONVENTION_FILE_NAME
    if not path.is_file():
        script_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(_CONVENTION_TEMPLATE, encoding="utf-8")
        raise ConventionError(
            f"Convention file created: {path}\nFill it in, then run Naming Check again."
        )
    settings = _read_settings(path)
    try:
        # Arguments are read in file order: the first setting to fill is named.
        return Convention(
            _setting(settings, "project", path),
            tuple(t.strip() for t in _setting(settings, "types", path).split(",")),
            _setting(settings, "music_type", path),
        )
    except ValueError as error:
        raise ConventionError(f"Invalid setting: {error}. File: {path}") from error


def run(client: WaapiClient, convention: Convention, apply: bool = False) -> Summary:
    """Audit the names of the SFX Sounds (Voice excluded) and Music Tracks.

    With ``apply``, the certain fixes are sent to Wwise in a single undo group;
    a rejected fix is recorded in ``failures`` and the batch continues. The
    project is never saved. If the connection is lost while applying, the fixes
    already sent stay applied in Wwise; a single undo in Wwise reverts them.

    Args:
        client: A connected WAAPI client. With ``allow_exception=True``, failures
            carry Wwise's reason; without it, they carry a generic message.
        convention: The project's naming convention.
        apply: Rename the objects whose fix is certain.

    Returns:
        The summary.

    Raises:
        WaapiRequestFailed: If Wwise rejects the query or the undo group and the
            client was created with ``allow_exception=True``.
        RequestRejected: Same, for a client created without it.
    """
    objects = _call(
        client,
        "ak.wwise.core.object.get",
        {"waql": _CHECKED_OBJECTS_QUERY},
        {"return": ["id", "type", "name"]},
    )["return"]
    issues = tuple(
        Issue(o["id"], o["type"], o["name"], reason, _certain_fix(o, convention))
        for o in objects
        if (reason := _violation(o["name"], convention))
    )
    summary = Summary(analyzed=len(objects), issues=issues)
    if not apply:
        return summary
    return Summary(summary.analyzed, issues, True, _apply(client, summary.fixable))


def main(argv: list[str] | None = None) -> int:
    """Run Naming Check on the project open in Wwise.

    Args:
        argv: Command-line arguments without the program name; defaults to
            ``sys.argv[1:]``.

    Returns:
        The process exit code: 0 if every name is compliant, 1 if some are
        not, 2 on error (Wwise unreachable, request rejected, convention file
        missing or invalid).
    """
    args = _parse_args(argv)
    try:
        client = WaapiClient(args.url, allow_exception=True)
    except CannotConnectToWaapiException:
        return _error(
            f"Error: cannot connect to Wwise at {args.url}. "
            "Is Wwise open with the Wwise Authoring API enabled?"
        )
    try:
        summary = _check_open_project(client, args.apply)
    except (ConventionError, RequestRejected) as error:
        return _error(str(error))
    except WaapiRequestFailed as error:
        return _error(f"Error: Wwise rejected a request: {_wwise_message(error)}")
    finally:
        client.disconnect()
    _print_summary(summary)
    return _EXIT_NON_COMPLIANT if summary.remaining else _EXIT_OK


def _read_settings(path: Path) -> configparser.SectionProxy:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(f"[{_INI_SECTION}]\n" + path.read_text(encoding="utf-8"))
    except configparser.DuplicateOptionError as error:
        raise ConventionError(
            f"Setting '{error.option}' appears twice: {path}"
        ) from error
    except configparser.Error as error:
        raise ConventionError(
            f"Every line must be '# comment' or 'setting = value': {path}"
        ) from error
    return parser[_INI_SECTION]


def _setting(settings: configparser.SectionProxy, key: str, path: Path) -> str:
    value = settings.get(key, "").strip()
    if not value or _PLACEHOLDER in value:
        raise ConventionError(f"Setting '{key}' is not filled in: {path}")
    return value


def _violation(name: str, convention: Convention) -> str | None:
    parts = name.split(_NAME_SEPARATOR)
    parts += [""] * (_REQUIRED_PARTS - len(parts))
    project, sound_type, category = parts[:_REQUIRED_PARTS]
    if project != convention.project:
        return f"does not start with project code '{convention.project}'"
    if sound_type not in convention.types:
        return f"missing or unknown type (allowed: {', '.join(convention.types)})"
    if not _CAMEL_CASE_WORD.fullmatch(category):
        return "category missing or not CamelCase"
    return None


def _certain_fix(obj: dict, convention: Convention) -> str | None:
    words = _words(obj["name"])
    if not words:
        return None
    if _matches(words[0].text, (convention.project,)) is None:
        words = _missing_prefix(words[0], obj["type"], convention) + words
    if len(words) < _REQUIRED_PARTS or _ambiguous_category(words):
        return None
    sound_type = _matches(words[1].text, convention.types)
    if sound_type is None:
        return None
    category = _camel_case(words[_REQUIRED_PARTS - 1].text)
    rest = [w.text for w in words[_REQUIRED_PARTS:]]
    parts = [convention.project, sound_type, category, *rest]
    fixed = _NAME_SEPARATOR.join(parts)
    return fixed if _violation(fixed, convention) is None else None


def _ambiguous_category(words: list[_Word]) -> bool:
    # Same "_" block: "Rats Scream" may be one category, or a category + suffix.
    category = _REQUIRED_PARTS - 1
    return (
        len(words) > _REQUIRED_PARTS
        and words[category].block == words[_REQUIRED_PARTS].block
    )


def _words(name: str) -> list[_Word]:
    words = [
        _Word(text, block)
        for block, chunk in enumerate(name.split(_NAME_SEPARATOR))
        for text in _WRONG_SEPARATORS.split(chunk)
    ]
    # An empty word means a doubled, leading or trailing separator: fix by hand.
    return [] if any(not w.text for w in words) else words


def _missing_prefix(
    first: _Word, object_type: str, convention: Convention
) -> list[_Word]:
    # Only Music Tracks have a known type when both prefixes are missing.
    if object_type == _MUSIC_TRACK and _matches(first.text, convention.types) is None:
        return [_Word(convention.project, -1), _Word(convention.music_type, -1)]
    return [_Word(convention.project, -1)]


def _matches(word: str, allowed: tuple[str, ...]) -> str | None:
    return next((a for a in allowed if a.upper() == word.upper()), None)


def _camel_case(word: str) -> str:
    return word[:1].upper() + word[1:]


def _apply(client: WaapiClient, issues: tuple[Issue, ...]) -> tuple[Failure, ...]:
    if not issues:
        return ()
    _call(client, "ak.wwise.core.undo.beginGroup")
    try:
        failures = tuple(filter(None, (_rename(client, i) for i in issues)))
    except BaseException:
        # Closing may fail too if the connection dropped: keep the original error.
        with contextlib.suppress(Exception):
            _end_undo_group(client)
        raise
    _end_undo_group(client)
    return failures


def _end_undo_group(client: WaapiClient) -> None:
    _call(client, "ak.wwise.core.undo.endGroup", {"displayName": _UNDO_GROUP_NAME})


def _rename(client: WaapiClient, issue: Issue) -> Failure | None:
    try:
        _call(
            client,
            "ak.wwise.core.object.setName",
            {"object": issue.object_id, "value": issue.fix},
        )
    except WaapiRequestFailed as error:
        return Failure(issue, _wwise_message(error))
    except RequestRejected as error:
        return Failure(issue, str(error))
    return None


def _check_open_project(client: WaapiClient, apply: bool) -> Summary:
    info = _call(client, "ak.wwise.core.getProjectInfo")
    script_dir = Path(info["path"]).parent / _SCRIPT_DIR_NAME
    return run(client, load_convention(script_dir), apply=apply)


def _call(
    client: WaapiClient, uri: str, args: dict | None = None, options: dict | None = None
) -> dict:
    result = client.call(uri, args or {}, options=options or {})
    if result is None:
        raise RequestRejected(
            f"Wwise rejected {uri} (create the client with allow_exception=True "
            "to get the reason)"
        )
    return result


def _wwise_message(error: WaapiRequestFailed) -> str:
    return (error.kwargs or {}).get("message", str(error))


def _error(message: str) -> int:
    print(message, file=sys.stderr)
    return _EXIT_ERROR


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=_DEFAULT_WAAPI_URL,
        help=f"WAAPI WebSocket URL (default: {_DEFAULT_WAAPI_URL})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="rename the objects whose fix is certain (default: audit only)",
    )
    return parser.parse_args(argv)


def _print_summary(summary: Summary) -> None:
    for issue in summary.issues:
        fix = f" -> fix: {issue.fix}" if issue.fix else " -> fix by hand"
        print(f"{issue.name}: {issue.reason}{fix}")
    print(f"{summary.analyzed} names checked")
    print(f"  {summary.compliant} compliant")
    print(f"  {len(summary.issues)} not compliant")
    print(f"    {len(summary.fixable)} with a certain fix")
    if not summary.applied:
        print(
            "Audit only: nothing renamed. Apply the fixes with --apply "
            "or Extensions > Naming Check > Apply fixes."
        )
        return
    print(f"{summary.renamed} names fixed")
    for failure in summary.failures:
        print(f"  FAILED {failure.issue.name}: {failure.reason}")
    print(f"{summary.remaining} names still to fix by hand")
    print("Project not saved: save it in Wwise to check out the Work Units.")


if __name__ == "__main__":
    sys.exit(main())
