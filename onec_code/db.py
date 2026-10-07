"""SQLite storage: schema, connections, helpers.

Layout
  contents          unique file contents (sha1). BSL text is stored once, zlib-compressed,
                    with its parse result; XML files keep only the parse result.
  content_methods   methods of each BSL content (dedup across snapshots); FTS rows.
  form_elements     all non-auxiliary elements of each form content (dedup).
  snapshots/files   what each snapshot consists of (path -> content).
  nodes/edges       per-snapshot graph. Method nodes point to content_methods.
  code_fts          FTS5, external content = view over content_methods + contents,
                    texts are not stored twice (view decompresses via bsl_slice()).
  names_fts         FTS5 over names/synonyms of metadata objects (external content = nodes).
"""
from __future__ import annotations

import os
import sqlite3
import threading
import zlib
from collections import OrderedDict

SCHEMA_VERSION = 1
FTS_TOKENIZE = "unicode61 remove_diacritics 0 tokenchars '_'"

# content_methods.flags
FLAG_EXPORT, FLAG_ASYNC, FLAG_FUNCTION, FLAG_IMPLICIT, FLAG_BROKEN = 1, 2, 4, 8, 16
FLAG_DATASET = 32          # "method" of a data composition schema: a data set, its text is the query
DIRECTIVES = [None, 'НаКлиенте', 'НаСервере', 'НаСервереБезКонтекста', 'НаКлиентеНаСервереБезКонтекста',
              'НаКлиентеНаСервере']
DIRECTIVE_CODE = {d: i for i, d in enumerate(DIRECTIVES)}


def directive_of(flags: int) -> str | None:
    c = flags >> 8
    return DIRECTIVES[c] if 0 <= c < len(DIRECTIVES) else None

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

-- AUTOINCREMENT: ids are never reused, so per-process caches keyed by snapshot/content id stay valid
CREATE TABLE IF NOT EXISTS snapshots(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label TEXT NOT NULL,
  label_norm TEXT NOT NULL UNIQUE,
  origin TEXT NOT NULL,              -- inbox | upload | dir
  source_path TEXT NOT NULL,
  source_size INTEGER, source_mtime INTEGER,
  config_name TEXT, config_synonym TEXT, config_version TEXT, vendor TEXT,
  is_extension INTEGER DEFAULT 0, name_prefix TEXT,
  base_snapshot_id INTEGER,          -- extensions: snapshot of the extended configuration
  graph_rev INTEGER DEFAULT 0,       -- bumped in the transaction that rebuilds nodes/edges (cache key)
  profile TEXT,                      -- configuration profile applied to the graph (NULL = universal)
  profile_override TEXT,             -- NULL = auto (by configuration name) | 'none' | profile code
  created_at TEXT, indexed_at TEXT,
  status TEXT NOT NULL DEFAULT 'queued',   -- queued | indexing | ready | error
  error TEXT, progress TEXT, stats TEXT,
  delete_source_after INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS contents(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  hash BLOB NOT NULL UNIQUE,         -- sha1 of the file bytes
  kind TEXT NOT NULL,                -- bsl | object | form | form_meta | config
  size INTEGER NOT NULL,
  lines INTEGER,
  pver INTEGER NOT NULL,             -- parser version the result was produced with
  ztext BLOB,                        -- bsl only: zlib(utf-8 text, BOM removed)
  parsed BLOB                        -- zlib(json) parse result
);

-- Hot columns only; params, regions, #Если, extension annotations, doc/header lines live
-- in contents.parsed ('m' list, same order as ord).
CREATE TABLE IF NOT EXISTS content_methods(
  id INTEGER PRIMARY KEY,
  content_id INTEGER NOT NULL,
  ord INTEGER NOT NULL,
  name TEXT NOT NULL,
  key TEXT NOT NULL,                 -- lower(name)
  flags INTEGER NOT NULL,            -- see FLAG_*; bits 8.. = directive code (DIRECTIVES index)
  line_start INTEGER, line_end INTEGER,
  cstart INTEGER, cend INTEGER       -- char offsets of doc_line..line_end in the text
);
CREATE INDEX IF NOT EXISTS cm_content ON content_methods(content_id, ord);
CREATE INDEX IF NOT EXISTS cm_key ON content_methods(key);

CREATE TABLE IF NOT EXISTS form_elements(
  content_id INTEGER NOT NULL,
  idx INTEGER NOT NULL,
  name TEXT NOT NULL,
  type TEXT NOT NULL,
  data_path TEXT,
  parent INTEGER,
  PRIMARY KEY(content_id, idx)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS files(
  snapshot_id INTEGER NOT NULL,
  path TEXT NOT NULL,
  crc INTEGER, size INTEGER, stamp INTEGER,
  content_id INTEGER NOT NULL,
  PRIMARY KEY(snapshot_id, path)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS files_content ON files(content_id);

CREATE TABLE IF NOT EXISTS nodes(
  id INTEGER PRIMARY KEY,
  snapshot_id INTEGER NOT NULL,
  kind TEXT NOT NULL,     -- config|object|attribute|tabular|enum_value|command|form|form_item|form_command|module|method
  type TEXT,              -- metadata type / attribute kind / module kind / element type
  name TEXT,              -- NULL for methods (see content_methods)
  key TEXT,               -- lower(name)
  full_name TEXT,         -- objects, forms, modules, commands
  synonym TEXT,
  parent_id INTEGER,
  content_id INTEGER,     -- module/form: contents.id; method: content_methods.id
  attrs TEXT              -- JSON
);
CREATE INDEX IF NOT EXISTS nodes_lookup ON nodes(snapshot_id, kind, key) WHERE key IS NOT NULL;
CREATE INDEX IF NOT EXISTS nodes_parent ON nodes(parent_id);
CREATE INDEX IF NOT EXISTS nodes_content ON nodes(content_id, kind) WHERE content_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS edges(
  id INTEGER PRIMARY KEY,
  snapshot_id INTEGER NOT NULL,
  from_id INTEGER NOT NULL,
  to_id INTEGER,                      -- NULL when unresolved / broken
  kind TEXT NOT NULL,
  confidence TEXT NOT NULL,           -- exact | heuristic | unresolved | broken
  line INTEGER,
  attrs TEXT
);
CREATE INDEX IF NOT EXISTS edges_from ON edges(from_id, kind);
CREATE INDEX IF NOT EXISTS edges_to ON edges(to_id, kind) WHERE to_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS edges_snap ON edges(snapshot_id, confidence, kind)
  WHERE confidence IN ('broken', 'unresolved');

CREATE VIEW IF NOT EXISTS code_fts_src AS
  SELECT cm.id AS id, bsl_slice(c.id, c.ztext, cm.cstart, cm.cend) AS body
  FROM content_methods cm JOIN contents c ON c.id = cm.content_id;

CREATE VIRTUAL TABLE IF NOT EXISTS code_fts USING fts5(
  body, content='code_fts_src', content_rowid='id', tokenize="{FTS_TOKENIZE}");

CREATE VIRTUAL TABLE IF NOT EXISTS names_fts USING fts5(
  name, synonym, content='nodes', content_rowid='id', tokenize="{FTS_TOKENIZE}");
"""


# ------------------------------------------------------------------ text access

class _TextCache:
    """Small LRU of decompressed module texts, shared by bsl_slice() and readers."""

    def __init__(self, size=64):
        self.size = size
        self.d: OrderedDict[int, str] = OrderedDict()
        self.lock = threading.Lock()

    def get(self, cid: int, ztext: bytes | None) -> str:
        with self.lock:
            t = self.d.get(cid)
            if t is not None:
                self.d.move_to_end(cid)
                return t
        t = zlib.decompress(ztext).decode('utf-8') if ztext else ''
        with self.lock:
            self.d[cid] = t
            if len(self.d) > self.size:
                self.d.popitem(last=False)
        return t

    def drop(self, cid: int):
        with self.lock:
            self.d.pop(cid, None)


TEXTS = _TextCache()


def _bsl_slice(cid, ztext, a, b):
    if ztext is None:
        return None
    return TEXTS.get(cid, ztext)[a:b]


def connect(path: str, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True, check_same_thread=False)
    else:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.create_function('bsl_slice', 4, _bsl_slice, deterministic=True)
    conn.execute('PRAGMA busy_timeout=60000')
    if not readonly:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA cache_size=-65536')      # 64 MB
    conn.execute('PRAGMA temp_store=MEMORY')
    conn.execute('PRAGMA mmap_size=268435456')
    conn.row_factory = sqlite3.Row
    return conn


def init(conn: sqlite3.Connection):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='contents'").fetchone():
        conn.execute('PRAGMA auto_vacuum=INCREMENTAL')    # must precede table creation
    conn.executescript(SCHEMA)
    # additive migrations of existing databases
    cols = {r[1] for r in conn.execute('PRAGMA table_info(snapshots)')}
    for col, decl in (('base_snapshot_id', 'INTEGER'), ('graph_rev', 'INTEGER DEFAULT 0'), ('profile', 'TEXT'),
                      ('profile_override', 'TEXT')):
        if col not in cols:
            conn.execute(f'ALTER TABLE snapshots ADD COLUMN {col} {decl}')
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))


def module_text(conn, content_id: int) -> str:
    row = conn.execute('SELECT ztext FROM contents WHERE id=?', (content_id,)).fetchone()
    return TEXTS.get(content_id, row[0]) if row else ''


def unjz(b: bytes):
    """Decode a zlib(json) parse result."""
    import json
    return json.loads(zlib.decompress(b)) if b else None


def norm_label(label: str) -> str:
    return ' '.join(label.split()).casefold()


class Tx:
    """BEGIN IMMEDIATE ... COMMIT/ROLLBACK for autocommit connections."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.execute('BEGIN IMMEDIATE')
        return self.conn

    def __exit__(self, et, ev, tb):
        self.conn.execute('COMMIT' if et is None else 'ROLLBACK')


def table_sizes(conn) -> list[tuple[str, int]]:
    """Bytes per table/index (dbstat), FTS shadow tables grouped under their FTS table."""
    try:
        rows = conn.execute('SELECT name, SUM(pgsize) FROM dbstat GROUP BY name').fetchall()
    except sqlite3.OperationalError:
        return []
    return sorted(((r[0], r[1]) for r in rows), key=lambda x: -x[1])
