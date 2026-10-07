"""Name resolution and labels for tools.

  config  -> snapshots   label (case/space-insensitive) | configuration name (latest version) | name@version
  text    -> node        'Документ.РеализацияТоваровУслуг', 'Document.X.ManagerModule',
                         'ОбщийМодуль.ОбщегоНазначения', 'ОбщегоНазначения.СообщитьПользователю',
                         'Документ.X.Форма.ФормаДокумента', 'Документ.X.Товары.Номенклатура',
                         synonyms ('Реализация товаров и услуг'), short module names ...
Nothing found -> NotFound with close matches.
"""
from __future__ import annotations

import difflib
import re
import threading
from dataclasses import dataclass

from . import db
from .layout import MODULE_KIND_ALIASES, TYPE_ALIASES


class NotFound(Exception):
    def __init__(self, what: str, suggestions=()):
        self.what = what
        self.suggestions = list(dict.fromkeys(suggestions))[:8]
        msg = what
        if self.suggestions:
            msg += '. Возможно, вы имели в виду: ' + '; '.join(self.suggestions)
        super().__init__(msg)


# ------------------------------------------------------------------ snapshots

@dataclass
class Snap:
    id: int
    label: str
    config_name: str
    version: str
    synonym: str
    status: str
    indexed_at: str
    graph_rev: int
    is_extension: bool

    @property
    def tag(self) -> str:
        return f'[{self.label} {self.version}]' if self.version else f'[{self.label}]'


def _vkey(v: str):
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r'[.\-]', v or '0'))


def all_snapshots(conn) -> list[Snap]:
    rows = conn.execute("SELECT id, label, config_name, config_version, config_synonym, status, indexed_at, "
                        "graph_rev, is_extension FROM snapshots WHERE indexed_at IS NOT NULL "
                        "AND status IN ('ready', 'indexing') ORDER BY config_name, id").fetchall()
    return [Snap(r[0], r[1], r[2] or '', r[3] or '', r[4] or '', r[5], r[6], r[7] or 0, bool(r[8])) for r in rows]


def snapshots(conn, config: str | None = None) -> list[Snap]:
    snaps = all_snapshots(conn)
    if not snaps:
        raise NotFound('Нет проиндексированных снимков (индексация ещё идёт или inbox пуст — см. index_status)')
    if not config or not config.strip():
        return snaps
    c = db.norm_label(config)
    hit = [s for s in snaps if db.norm_label(s.label) == c]
    if hit:
        return hit
    if '@' in c:
        name, _, ver = c.partition('@')
        hit = [s for s in snaps if s.config_name.casefold() == name.strip() and s.version == ver.strip()]
        if hit:
            return hit
    by_name = [s for s in snaps if s.config_name.casefold() == c or db.norm_label(s.synonym) == c]
    if by_name:
        best = max(by_name, key=lambda s: (_vkey(s.version), s.indexed_at))
        return [best]
    cands = [s.label for s in snaps] + [s.config_name for s in snaps] + \
            [f'{s.config_name}@{s.version}' for s in snaps]
    close = difflib.get_close_matches(config, cands, n=5, cutoff=0.4)
    raise NotFound(f'Конфигурация «{config}» не найдена', close or cands[:6])


def one_snapshot(conn, config: str | None, what='') -> Snap:
    snaps = snapshots(conn, config)
    if len(snaps) == 1:
        return snaps[0]
    raise NotFound(f'Укажите config{(" для " + what) if what else ""}: загружено несколько снимков',
                   [s.label for s in snaps])


# ------------------------------------------------------------------ catalog cache

class Catalog:
    """Objects, modules, forms and commands of one snapshot (methods/attributes stay in SQL)."""

    def __init__(self, conn, sid):
        self.sid = sid
        self.by_full: dict[str, int] = {}
        self.kind: dict[int, str] = {}
        self.obj: dict[tuple[str, str], int] = {}
        self.obj_by_key: dict[str, list[int]] = {}
        self.obj_by_syn: dict[str, list[int]] = {}
        self.objects: list[tuple] = []          # (id, type, name, synonym)
        self.synonym: dict[int, str] = {}
        self.full: dict[int, str] = {}
        self.modules_of: dict[int, list[tuple[int, str]]] = {}   # owner -> [(module id, kind)]
        self.config_id = None
        for r in conn.execute("SELECT id, kind, type, name, key, full_name, synonym, parent_id FROM nodes "
                              "WHERE snapshot_id=? AND kind IN ('config','object','form','command','module')",
                              (sid,)):
            nid, kind = r[0], r[1]
            self.kind[nid] = kind
            if r[5]:
                self.by_full[r[5].lower()] = nid
                self.full[nid] = r[5]
            if kind == 'object':
                self.obj[(r[2], r[4])] = nid
                self.obj_by_key.setdefault(r[4], []).append(nid)
                if r[6]:
                    self.obj_by_syn.setdefault(r[6].casefold(), []).append(nid)
                    self.synonym[nid] = r[6]
                self.objects.append((nid, r[2], r[3], r[6] or ''))
            elif kind == 'module':
                self.modules_of.setdefault(r[7], []).append((nid, r[2]))
            elif kind == 'config':
                self.config_id = nid


_CACHE: dict[tuple[str, int], tuple[int, Catalog]] = {}     # (db file, snapshot id) -> (graph_rev, catalog)
_CACHE_LOCK = threading.Lock()


def catalog(conn, snap: Snap) -> Catalog:
    rev = conn.execute('SELECT graph_rev FROM snapshots WHERE id=?', (snap.id,)).fetchone()
    rev = rev[0] if rev else -1
    key = (conn.execute('PRAGMA database_list').fetchone()[2], snap.id)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] == rev:
            return hit[1]
    cat = Catalog(conn, snap.id)
    with _CACHE_LOCK:
        _CACHE[key] = (rev, cat)
        if len(_CACHE) > 12:
            _CACHE.pop(next(iter(_CACHE)))
    return cat


# ------------------------------------------------------------------ node resolution

DATA_TYPES = {'Документ', 'Справочник', 'РегистрНакопления', 'РегистрСведений', 'РегистрБухгалтерии',
              'РегистрРасчета', 'ПланВидовХарактеристик', 'ПланСчетов', 'ПланВидовРасчета', 'Перечисление',
              'БизнесПроцесс', 'Задача', 'ПланОбмена', 'Обработка', 'Отчет', 'ЖурналДокументов', 'Константа'}
FORM_WORDS = {'форма', 'form', 'формы', 'forms'}
COMMAND_WORDS = {'команда', 'command', 'команды', 'commands'}
TEMPLATE_WORDS = {'макет', 'template', 'макеты', 'templates'}
DCS_TYPE = 'СхемаКомпоновкиДанных'
MODULE_WORD = {'модуль', 'module'}
OBJECT_MODULE_ORDER = ['МодульОбъекта', 'МодульМенеджера', 'МодульНабораЗаписей', 'МодульМенеджераЗначения',
                       'Модуль', 'МодульКоманды']


def split_name(text: str) -> list[str]:
    t = re.sub(r'\s*\.\s*', '.', (text or '').strip())
    t = re.sub(r'\(.*$', '', t)          # "Метод()" / signatures
    return [p for p in t.split('.') if p]


@dataclass
class Resolved:
    id: int
    kind: str          # object|module|method|form|command|attribute|tabular|enum_value|config|form_item
    label: str


def _close(word, options, n=6):
    low = {o.lower(): o for o in options}
    return [low[x] for x in difflib.get_close_matches(word.lower(), list(low), n=n, cutoff=0.55)]


def _object_suggestions(cat: Catalog, text: str):
    fulls = [f'{t}.{n}' for _, t, n, _ in cat.objects]
    s = _close(text, fulls)
    name = text.split('.')[-1]
    s += _close(name, [n for _, _, n, _ in cat.objects])
    low = name.casefold()
    if len(low) >= 4:
        s += [f'{t}.{n}' for _, t, n, syn in cat.objects if low in n.casefold() or low in syn.casefold()][:6]
    return s


def resolve_object(conn, snap: Snap, parts: list[str]) -> tuple[int, list[str]]:
    """Consume the object part of a dotted name. Returns (object/config node id, remaining parts)."""
    cat = catalog(conn, snap)
    if not parts:
        raise NotFound('Пустое имя')
    p0 = parts[0].lower()
    if p0 in MODULE_KIND_ALIASES and MODULE_KIND_ALIASES[p0] in (
            'МодульСеанса', 'МодульУправляемогоПриложения', 'МодульОбычногоПриложения', 'МодульВнешнегоСоединения'):
        return cat.config_id, parts
    t = TYPE_ALIASES.get(p0)
    if t == 'Конфигурация':
        return cat.config_id, parts[1:]
    if t and len(parts) >= 2:
        oid = cat.obj.get((t, parts[1].lower()))
        if oid is None:
            syn = [i for i in cat.obj_by_syn.get(parts[1].casefold(), []) if cat.full[i].startswith(t + '.')]
            oid = syn[0] if len(syn) == 1 else None
        if oid is None:
            raise NotFound(f'{snap.tag} Объект «{t}.{parts[1]}» не найден', _object_suggestions(cat, f'{t}.{parts[1]}'))
        return oid, parts[2:]
    # no type: name or synonym of any object
    ids = cat.obj_by_key.get(p0) or cat.obj_by_syn.get(parts[0].casefold()) or []
    if len(ids) == 1:
        return ids[0], parts[1:]
    if len(ids) > 1:
        cm = [i for i in ids if cat.full[i].startswith('ОбщийМодуль.')]
        if len(cm) == 1:
            return cm[0], parts[1:]
        # same names are common (picture/role/subsystem named like a document): prefer a data object
        data = [i for i in ids if cat.full[i].split('.')[0] in DATA_TYPES]
        if len(data) == 1:
            return data[0], parts[1:]
        raise NotFound(f'{snap.tag} Имя «{parts[0]}» неоднозначно, укажите тип', [cat.full[i] for i in ids])
    raise NotFound(f'{snap.tag} Объект «{parts[0]}» не найден', _object_suggestions(cat, parts[0]))


def _method_in_module(conn, module_id: int, name: str):
    r = conn.execute("SELECT n.id FROM nodes n JOIN content_methods cm ON cm.id = n.content_id "
                     "WHERE n.parent_id=? AND n.kind='method' AND cm.key=?", (module_id, name.lower())).fetchone()
    return r[0] if r else None


def _method_names(conn, module_id: int) -> list[str]:
    return [r[0] for r in conn.execute("SELECT cm.name FROM nodes n JOIN content_methods cm ON cm.id=n.content_id "
                                       "WHERE n.parent_id=? AND n.kind='method'", (module_id,))]


def _child(conn, parent, kind, name):
    r = conn.execute('SELECT id FROM nodes WHERE parent_id=? AND kind=? AND key=?',
                     (parent, kind, name.lower())).fetchone()
    return r[0] if r else None


def _modules_of(cat: Catalog, owner: int) -> list[tuple[int, str]]:
    mods = cat.modules_of.get(owner, [])
    return sorted(mods, key=lambda m: OBJECT_MODULE_ORDER.index(m[1]) if m[1] in OBJECT_MODULE_ORDER else 99)


def resolve_node(conn, snap: Snap, text: str, want: str | None = None) -> Resolved:
    """Resolve a dotted name to a node. want: 'module' | 'method' | 'object' | 'form' | None (any)."""
    parts = split_name(text)
    cat = catalog(conn, snap)
    oid, rest = resolve_object(conn, snap, parts)
    owner_full = cat.full.get(oid, 'Конфигурация')
    cur, kind = oid, cat.kind.get(oid, 'object')

    def module_of(owner):
        mods = _modules_of(cat, owner)
        if not mods:
            raise NotFound(f'{snap.tag} У «{cat.full.get(owner)}» нет модулей')
        return mods

    i = 0
    while i < len(rest):
        w = rest[i]
        lw = w.lower()
        if kind in ('object', 'config', 'form', 'command') and lw in MODULE_KIND_ALIASES:
            mk = MODULE_KIND_ALIASES[lw]
            m = [x for x in cat.modules_of.get(cur, []) if x[1] == mk]
            if not m:
                raise NotFound(f'{snap.tag} У «{cat.full.get(cur, owner_full)}» нет модуля {mk}',
                               [cat.full[x[0]] for x in module_of(cur)])
            cur, kind = m[0][0], 'module'
        elif kind == 'object' and lw in FORM_WORDS and i + 1 < len(rest):
            f = cat.by_full.get(f'{owner_full}.форма.{rest[i + 1]}'.lower())
            if f is None:
                forms = [v for k, v in cat.full.items() if v.startswith(owner_full + '.Форма.')]
                raise NotFound(f'{snap.tag} Форма «{rest[i + 1]}» у «{owner_full}» не найдена', _close(
                    f'{owner_full}.Форма.{rest[i + 1]}', forms) or forms)
            cur, kind = f, 'form'
            i += 1
        elif kind == 'object' and lw in TEMPLATE_WORDS and i + 1 < len(rest):
            t = cat.by_full.get(f'{owner_full}.макет.{rest[i + 1]}'.lower())
            if t is None:
                dcs = [cat.full[m] for m, mk in _modules_of(cat, cur) if mk == DCS_TYPE]
                raise NotFound(f'{snap.tag} Схема СКД «{rest[i + 1]}» у «{owner_full}» не найдена (индексируются '
                               'только макеты-схемы компоновки)', _close(f'{owner_full}.Макет.{rest[i + 1]}', dcs) or dcs)
            cur, kind = t, 'module'
            i += 1
        elif kind == 'object' and lw in COMMAND_WORDS and i + 1 < len(rest):
            c = cat.by_full.get(f'{owner_full}.команда.{rest[i + 1]}'.lower())
            if c is None:
                raise NotFound(f'{snap.tag} Команда «{rest[i + 1]}» у «{owner_full}» не найдена')
            cur, kind = c, 'command'
            i += 1
        elif kind == 'module':
            m = _method_in_module(conn, cur, w)
            if m is None:
                raise NotFound(f'{snap.tag} Метод «{w}» в модуле «{cat.full[cur]}» не найден',
                               [f'{short_module(cat.full[cur])}.{x}' for x in _close(w, _method_names(conn, cur))])
            cur, kind = m, 'method'
        elif kind in ('form', 'command', 'config') or (kind == 'object' and cat.full[cur].split('.')[0] in (
                'ОбщийМодуль', 'ОбщаяФорма', 'ОбщаяКоманда', 'ОбщийМакет')):
            # form command / item: Форма.Ф.Команды.Имя, Форма.Ф.Элементы.Имя, or just Форма.Ф.Имя
            if kind == 'form' and lw in ('команды', 'commands', 'элементы', 'items') and i + 1 < len(rest):
                ck = 'form_command' if lw in ('команды', 'commands') else 'form_item'
                c = _child(conn, cur, ck, rest[i + 1])
                if c is None:
                    names = [r[0] for r in conn.execute('SELECT name FROM nodes WHERE parent_id=? AND kind=?', (cur, ck))]
                    raise NotFound(f'{snap.tag} «{rest[i + 1]}» нет среди {"команд" if ck == "form_command" else "элементов с обработчиками"} '
                                   f'формы {cat.full[cur]}', _close(rest[i + 1], names))
                return Resolved(c, ck, labels(conn, [c])[c])
            if kind == 'form' and i == len(rest) - 1 and want != 'method':
                for ck in ('form_item', 'form_command'):
                    c = _child(conn, cur, ck, w)
                    if c is not None:
                        return Resolved(c, ck, labels(conn, [c])[c])
            mods = module_of(cur)
            m = _method_in_module(conn, mods[0][0], w)
            if m is None:
                raise NotFound(f'{snap.tag} «{w}» не найден в «{cat.full.get(cur, owner_full)}»',
                               [f'{short_module(cat.full[mods[0][0]])}.{x}'
                                for x in _close(w, _method_names(conn, mods[0][0]))])
            cur, kind = m, 'method'
        elif kind == 'object':
            a = _child(conn, cur, 'attribute', w)
            ts = _child(conn, cur, 'tabular', w)
            ev = _child(conn, cur, 'enum_value', w)
            if ts is not None:
                if i + 1 < len(rest):
                    ta = _child(conn, ts, 'attribute', rest[i + 1])
                    if ta is None:
                        raise NotFound(f'{snap.tag} Реквизит «{rest[i + 1]}» табличной части «{w}» не найден')
                    return Resolved(ta, 'attribute', f'{owner_full}.{w}.{rest[i + 1]}')
                cur, kind = ts, 'tabular'
            elif a is not None:
                cur, kind = a, 'attribute'
            elif ev is not None:
                cur, kind = ev, 'enum_value'
            else:
                # method of one of the object's modules
                found = [(mid, mk, _method_in_module(conn, mid, w)) for mid, mk in _modules_of(cat, cur)]
                found = [f for f in found if f[2]]
                if len(found) == 1 or (found and want == 'method'):
                    cur, kind = found[0][2], 'method'
                elif len(found) > 1:
                    raise NotFound(f'{snap.tag} Метод «{w}» есть в нескольких модулях «{owner_full}», уточните',
                                   [f'{short_module(cat.full[f[0]])}.{w}' for f in found])
                else:
                    attrs = [r[0] for r in conn.execute(
                        "SELECT name FROM nodes WHERE parent_id=? AND kind IN ('attribute','tabular')", (cur,))]
                    meths = [x for mid, _ in _modules_of(cat, cur) for x in _method_names(conn, mid)]
                    raise NotFound(f'{snap.tag} «{w}» не найден в «{owner_full}» (ни реквизит, ни метод)',
                                   [f'{owner_full}.{x}' for x in _close(w, attrs + meths)])
        else:
            raise NotFound(f'{snap.tag} Не понимаю часть имени «{w}» в «{text}»')
        i += 1

    if want == 'module' and kind in ('object', 'form', 'command', 'config'):
        mods = module_of(cur)
        mods = [m for m in mods if m[1] != DCS_TYPE] or mods      # code modules first, schemas via .Макет.Имя
        if len(mods) > 1 and kind in ('object', 'config'):
            raise NotFound(f'{snap.tag} У «{cat.full.get(cur, owner_full)}» несколько модулей, укажите какой',
                           [cat.full[m[0]] for m in mods])
        cur, kind = mods[0][0], 'module'
    if want == 'object' and kind not in ('object', 'config'):
        raise NotFound(f'{snap.tag} «{text}» — это не объект метаданных')
    return Resolved(cur, kind, labels(conn, [cur])[cur])


def resolve_method(conn, snap: Snap, module: str | None, name: str) -> Resolved:
    if module:
        return resolve_node(conn, snap, f'{module}.{name}', want='method')
    return resolve_node(conn, snap, name, want='method')


# ------------------------------------------------------------------ labels

def short_module(full: str) -> str:
    """'ОбщийМодуль.X.Модуль' -> 'ОбщийМодуль.X'; 'Документ.X.Форма.Ф.Модуль' -> 'Документ.X.Форма.Ф'."""
    return full[:-7] if full.endswith('.Модуль') else full


def labels(conn, ids) -> dict[int, str]:
    ids = [i for i in set(ids) if i is not None]
    out = {}
    for k in range(0, len(ids), 500):
        part = ids[k:k + 500]
        for r in conn.execute(
                'SELECT n.id, n.kind, n.name, n.full_name, p.kind, p.name, p.full_name, pp.full_name, cm.name '
                'FROM nodes n LEFT JOIN nodes p ON p.id = n.parent_id LEFT JOIN nodes pp ON pp.id = p.parent_id '
                "LEFT JOIN content_methods cm ON n.kind = 'method' AND cm.id = n.content_id "
                f'WHERE n.id IN ({",".join("?" * len(part))})', part):
            nid, kind, name, full, pkind, pname, pfull, ppfull, mname = r
            if kind == 'method':
                out[nid] = f'{short_module(pfull or "")}.{mname}'
            elif full:
                out[nid] = full
            elif pkind == 'tabular':
                out[nid] = f'{ppfull}.{pname}.{name}'
            elif kind == 'form_item':
                out[nid] = f'{pfull}.Элементы.{name}'
            elif kind == 'form_command':
                out[nid] = f'{pfull}.Команды.{name}'
            else:
                out[nid] = f'{pfull}.{name}' if pfull else (name or str(nid))
    return out


def fmt_type(t: str) -> str:
    kind, _, v = t.partition(':')
    if kind in ('ref', 'obj', 'prim'):
        return v
    if kind == 'def':
        return f'ОпределяемыйТип.{v}'
    if kind == 'char':
        return f'Характеристика.{v}'
    if kind == 'any':
        return f'ЛюбаяСсылка({v})'
    if t == 'anyref':
        return 'ЛюбаяСсылка'
    return v or t


def fmt_types(types) -> str:
    return ', '.join(fmt_type(t) for t in types) or '—'
