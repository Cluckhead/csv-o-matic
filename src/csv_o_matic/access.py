"""Confined, read-only access to approved folders and their genuine subfolders.

The process maps name the approved roots. Everything else is reached from a root one path
component at a time, and no component may be a link: not a symbolic link, not a Windows
junction or other reparse point. A file is opened relative to the directory that was
reached, never by re-resolving a path string, so swapping a directory for a link between
the check and the open cannot redirect the read (where the platform supports it; see
`HAVE_DIR_FD`). When confinement cannot be shown, access is refused.
"""

from __future__ import annotations

import errno
import os
import stat as stat_module
from pathlib import Path
from typing import Iterator

# Directory-relative opens that refuse links: available on Linux and macOS, not on Windows.
HAVE_DIR_FD = (os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd and os.scandir in os.supports_fd
               and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"))
_REPARSE_POINT = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class AccessError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def _is_link(st: os.stat_result) -> bool:
    return stat_module.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def _bad_component(name: str) -> bool:
    return (not name or name in (os.curdir, os.pardir) or "\x00" in name or "/" in name
            or (os.sep != "/" and os.sep in name) or bool(os.altsep and os.altsep in name))


class ConfinedDir:
    """A directory inside an approved root, reached without following any link below the root.

    The root itself comes from trusted configuration and is used as resolved there. Use as a
    context manager; every operation is relative to the directory actually reached.
    """

    def __init__(self, root: Path, parts: tuple[str, ...], shown: str):
        self.root = str(root)
        self.parts = parts
        self.shown = shown  # how the folder is named in messages
        self._fd: int | None = None
        self._path = os.path.join(self.root, *parts)
        for part in parts:
            if _bad_component(part):
                raise AccessError("folder_not_found", f"'{shown}' is not a valid folder path.")
        if HAVE_DIR_FD:
            self._fd = self._descend()
        else:
            self._verify_chain()

    def __enter__(self) -> "ConfinedDir":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    # ------------------------------------------------------------ reaching the directory

    def _descend(self) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(self.root, flags)
        except OSError as exc:
            raise self._folder_error(exc, None, None) from exc
        try:
            for part in self.parts:
                try:
                    below = os.open(part, flags, dir_fd=fd)
                except OSError as exc:
                    raise self._folder_error(exc, part, fd) from exc
                os.close(fd)
                fd = below
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _folder_error(self, exc: OSError, part: str | None, parent: int | None) -> AccessError:
        if part is not None and parent is not None:
            try:
                if _is_link(os.stat(part, dir_fd=parent, follow_symlinks=False)):
                    return self._link_error(part)
            except OSError:
                pass
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            return AccessError("folder_not_found", f"Folder '{self.shown}' does not exist on disk"
                               + (f" ('{part}' is not a directory there)." if part else "."))
        if exc.errno == errno.ELOOP:
            return self._link_error(part or self.shown)
        return AccessError("folder_unreadable", f"Cannot open folder '{self.shown}': {exc.strerror or exc}.")

    def _link_error(self, part: str) -> AccessError:
        return AccessError(
            "linked_folder_not_followed",
            f"'{part}' in '{self.shown}' is a link (symbolic link, junction or mount-style redirect). Links are "
            "never followed, because they can lead outside the approved folders. Nothing was read.")

    def _verify_chain(self) -> None:
        """Fallback without directory-relative opens: every component must be a plain directory, and the
        path must really be where it is spelled."""
        path = self.root
        try:
            if not stat_module.S_ISDIR(os.stat(path).st_mode):
                raise AccessError("folder_not_found", f"Folder '{self.shown}' does not exist on disk.")
            for part in self.parts:
                path = os.path.join(path, part)
                st = os.lstat(path)
                if _is_link(st) or getattr(os.path, "isjunction", lambda _p: False)(path):
                    raise self._link_error(part)
                if not stat_module.S_ISDIR(st.st_mode):
                    raise AccessError("folder_not_found", f"Folder '{self.shown}' does not exist on disk "
                                                          f"('{part}' is not a directory there).")
            real = os.path.realpath(path)
        except FileNotFoundError:
            raise AccessError("folder_not_found", f"Folder '{self.shown}' does not exist on disk.") from None
        except OSError as exc:
            raise AccessError("folder_unreadable", f"Cannot open folder '{self.shown}': {exc.strerror or exc}.") from exc
        expected = os.path.join(os.path.realpath(self.root), *self.parts)
        if os.path.normcase(real) != os.path.normcase(expected):
            raise AccessError("linked_folder_not_followed",
                              f"'{self.shown}' does not resolve to a location inside its approved folder. Nothing was read.")

    # ------------------------------------------------------------ operations inside it

    def scandir(self) -> Iterator[os.DirEntry]:
        """Entries of the directory. Type checks on them must pass follow_symlinks=False."""
        if self._fd is not None:
            return os.scandir(self._fd)
        self._verify_chain()
        return os.scandir(self._path)

    def lstat(self, name: str) -> os.stat_result:
        if self._fd is not None:
            return os.stat(name, dir_fd=self._fd, follow_symlinks=False)
        return os.lstat(os.path.join(self._path, name))

    def open_file(self, name: str) -> tuple[int, os.stat_result]:
        """Open a regular file in this directory read-only, refusing links and special files.

        Raises FileNotFoundError if there is no such entry, AccessError for anything that must not be
        read. O_NONBLOCK keeps a FIFO swapped in at the last moment from blocking the open.
        """
        if _bad_component(name):
            raise AccessError("invalid_argument", "filename must be a bare file name inside the folder, without any path.")
        flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
                 | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOCTTY", 0))
        not_regular = AccessError("file_unreadable", f"'{name}' is not a regular file (links and special files are not read).")
        try:
            before = self.lstat(name)
            if _is_link(before) or not stat_module.S_ISREG(before.st_mode):
                raise not_regular
            if self._fd is not None:
                fd = os.open(name, flags, dir_fd=self._fd)
            else:
                fd = os.open(os.path.join(self._path, name), flags)
        except FileNotFoundError:
            raise
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise not_regular from None
            raise AccessError("file_unreadable", f"Cannot open '{name}' for reading: {exc.strerror or exc}.") from exc
        try:
            st = os.fstat(fd)
            if not stat_module.S_ISREG(st.st_mode):
                raise not_regular
            if (st.st_dev, st.st_ino) != (before.st_dev, before.st_ino):
                raise AccessError("file_unreadable", f"'{name}' was replaced while it was being opened; try again.")
            if self._fd is None:
                self._verify_chain()  # nothing on the way here may have been swapped for a link meanwhile
            return fd, st
        except BaseException:
            os.close(fd)
            raise
