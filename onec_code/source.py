"""Read-only access to a configuration dump: a zip archive or a folder.

Only the files the indexer needs are listed (see layout.classify); nothing is
ever extracted or written next to the source.
"""
from __future__ import annotations

import os
import zipfile
import zlib
from dataclasses import dataclass

from .layout import Entry, classify


@dataclass(frozen=True)
class SourceFile:
    entry: Entry
    crc: int          # CRC32 (zip) or CRC32 computed lazily for folders (-1 until read)
    size: int
    stamp: int        # change marker: CRC32 for zip, mtime_ns for folders


class Source:
    kind = ''

    def files(self) -> list[SourceFile]:
        raise NotImplementedError

    def read(self, path: str) -> bytes:
        raise NotImplementedError

    def head(self, path: str, n: int) -> bytes:
        return self.read(path)[:n]

    def has_configuration(self) -> bool:
        raise NotImplementedError

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class ZipSource(Source):
    kind = 'zip'

    def __init__(self, path: str):
        self.path = path
        self._z = zipfile.ZipFile(path)
        # real name -> ZipInfo. Archives made by the Windows 10 "Compressed (zipped) folder" store
        # Cyrillic names in cp866 without the UTF-8 flag; zipfile then decodes them as cp437.
        self._infos: dict[str, zipfile.ZipInfo] = {}
        for zi in self._z.infolist():
            name = zi.filename
            if not zi.flag_bits & 0x800 and not name.isascii():
                try:
                    name = name.encode('cp437').decode('cp866')
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
            self._infos[name.replace('\\', '/')] = zi
        self._prefix = self._detect_prefix()

    def _detect_prefix(self) -> str:
        """Dumps are sometimes zipped together with their folder: 'ut/Configuration.xml'."""
        best = None
        for n in self._infos:
            if n == 'Configuration.xml' or n.endswith('/Configuration.xml'):
                pre = n[: -len('Configuration.xml')]
                if best is None or len(pre) < len(best):
                    best = pre
        return best or ''

    def has_configuration(self) -> bool:
        return (self._prefix + 'Configuration.xml') in self._infos

    def files(self) -> list[SourceFile]:
        out = []
        pre = self._prefix
        for name, zi in self._infos.items():
            if zi.is_dir() or not name.startswith(pre):
                continue
            e = classify(name[len(pre):])
            if e is not None:
                out.append(SourceFile(e, zi.CRC, zi.file_size, zi.CRC))
        return out

    def read(self, path: str) -> bytes:
        return self._z.read(self._infos[self._prefix + path])

    def head(self, path: str, n: int) -> bytes:
        """First n bytes (only that much is decompressed)."""
        with self._z.open(self._infos[self._prefix + path]) as f:
            return f.read(n)

    def close(self):
        self._z.close()


class DirSource(Source):
    kind = 'dir'

    def __init__(self, path: str):
        self.path = path

    def has_configuration(self) -> bool:
        return os.path.isfile(os.path.join(self.path, 'Configuration.xml'))

    def files(self) -> list[SourceFile]:
        out = []
        root = self.path
        for d, _dirs, names in os.walk(root):
            rel_dir = os.path.relpath(d, root).replace(os.sep, '/')
            for fn in names:
                rel = fn if rel_dir == '.' else f'{rel_dir}/{fn}'
                e = classify(rel)
                if e is None:
                    continue
                st = os.stat(os.path.join(d, fn))
                out.append(SourceFile(e, -1, st.st_size, st.st_mtime_ns))
        return out

    def read(self, path: str) -> bytes:
        with open(os.path.join(self.path, *path.split('/')), 'rb') as f:
            return f.read()

    def head(self, path: str, n: int) -> bytes:
        with open(os.path.join(self.path, *path.split('/')), 'rb') as f:
            return f.read(n)


def open_source(path: str) -> Source:
    if os.path.isdir(path):
        return DirSource(path)
    return ZipSource(path)


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF
