"""MCP tool implementations. Every function takes a DB connection and returns compact text.

Graph tools return names, edge kinds, confidence and counts only; code text is returned
only by get_procedure / get_module.
"""
from __future__ import annotations

import difflib
import json
import re
from collections import OrderedDict, defaultdict

from . import db, graph, profiles
from .db import FLAG_ASYNC, FLAG_DATASET, FLAG_EXPORT, FLAG_FUNCTION, FLAG_IMPLICIT, directive_of
from .layout import TYPE_ALIASES, classify
from .md_parser import FORM_EVENTS, SUBSCRIPTION_EVENTS
from .query import (NotFound, Snap, catalog, fmt_types, labels, one_snapshot, resolve_method, resolve_node,
                    short_module, snapshots, split_name)

SERVICE = None      # set by the server: IndexService (reindex / index_status)

CALLER_KINDS = ('calls', 'handler', 'event_subscription', 'form_handler', 'scheduled_job', 'extension_intercepts')


# ------------------------------------------------------------------ helpers

class Out:
    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, s=''):
        self.lines.append(s)

    def text(self):
        return '\n'.join(self.lines).rstrip() + '\n'


_PARSED: OrderedDict[int, tuple[int, dict]] = OrderedDict()


def parsed_of(conn, content_id: int) -> dict:
    """Parse result of a content (LRU; revalidated by parser version — re-parsing keeps the id)."""
    row = conn.execute('SELECT pver FROM contents WHERE id=?', (content_id,)).fetchone()
    if row is None:
        return {}
    hit = _PARSED.get(content_id)
    if hit and hit[0] == row[0]:
        _PARSED.move_to_end(content_id)
        return hit[1]
    p = db.unjz(conn.execute('SELECT parsed FROM contents WHERE id=?', (content_id,)).fetchone()[0]) or {}
    _PARSED[content_id] = (row[0], p)
    if len(_PARSED) > 256:
        _PARSED.popitem(last=False)
    return p


def method_row(conn, method_node: int):
    """(content_method row, module node id, module full name)"""
    r = conn.execute('SELECT cm.*, n.parent_id AS module_id, p.full_name AS module_full, p.content_id AS module_content '
                     'FROM nodes n JOIN content_methods cm ON cm.id = n.content_id '
                     'JOIN nodes p ON p.id = n.parent_id WHERE n.id=?', (method_node,)).fetchone()
    return r


def signature(conn, cm) -> str:
    det = parsed_of(conn, cm['content_id']).get('m', [])
    params = det[cm['ord']][0] if cm['ord'] < len(det) else ''
    f = cm['flags']
    if f & FLAG_DATASET:
        return f"Набор данных {cm['name']} ({params})"
    s = ('Асинх ' if f & FLAG_ASYNC else '') + ('Функция ' if f & FLAG_FUNCTION else 'Процедура ') + \
        f"{cm['name']}({params})" + (' Экспорт' if f & FLAG_EXPORT else '')
    d = directive_of(f)
    if d:
        s = f'&{d}{" (неявно)" if f & FLAG_IMPLICIT else ""} ' + s
    return s


def method_details(conn, cm) -> dict:
    det = parsed_of(conn, cm['content_id']).get('m', [])
    if cm['ord'] >= len(det):
        return {}
    d = det[cm['ord']]
    return {'params': d[0], 'regions': d[1], 'ifdef': d[2], 'annotations': d[3], 'ext_kind': d[4],
            'ext_target': d[5], 'doc_line': d[6], 'line_header': d[7]}


def tags(snaps) -> str:
    return ' '.join(s.tag for s in snaps)


def _kinds(edge_kinds) -> list[str]:
    if not edge_kinds:
        return list(graph.KINDS)
    if isinstance(edge_kinds, str):
        edge_kinds = re.split(r'[,\s]+', edge_kinds)
    ks = [k.strip() for k in edge_kinds if k.strip()]
    bad = [k for k in ks if k not in graph.KINDS]
    if bad:
        raise NotFound(f'Неизвестные виды связей: {", ".join(bad)}', graph.KINDS)
    return ks


def _per_snapshot(conn, config, fn):
    """Run fn(snap) for every snapshot in scope; NotFound in all of them -> re-raise the first one."""
    results, errors = [], []
    for s in snapshots(conn, config):
        try:
            results.append((s, fn(s)))
        except NotFound as e:
            errors.append(e)
    if not results and errors:
        raise errors[0]
    return results


def _edge_attrs(a):
    return json.loads(a) if a else {}


def _conf_mark(conf, attrs=None) -> str:
    m = {'exact': '', 'heuristic': ' ~эвристика', 'unresolved': ' ?неразрешено', 'broken': ' ✗битая'}[conf]
    if attrs and attrs.get('dynamic'):
        m += f' (динамически: {attrs["dynamic"]})'
    return m


EVENT_ALIASES = {}
for _en, _ru in list(SUBSCRIPTION_EVENTS.items()) + list(FORM_EVENTS.items()):
    EVENT_ALIASES[_en.lower()] = _ru
    EVENT_ALIASES[_ru.lower()] = _ru


def event_ru(name: str) -> str:
    return EVENT_ALIASES.get((name or '').lower(), name)


# ------------------------------------------------------------------ catalog

def list_configs(conn) -> str:
    rows = conn.execute('SELECT * FROM snapshots ORDER BY config_name, config_version, label').fetchall()
    if not rows:
        return 'Снимков нет. Положите zip в inbox или загрузите через веб-интерфейс (порт 8003).\n'
    o = Out()
    groups = defaultdict(list)
    for r in rows:
        groups[(r['config_name'] or '(ещё не прочитано)', r['config_synonym'] or '')].append(r)
    for (name, syn), rs in groups.items():
        o(f'{name}' + (f' — «{syn}»' if syn else ''))
        for r in rs:
            st = json.loads(r['stats']) if r['stats'] else {}
            ext = ' расширение' if r['is_extension'] else ''
            status = {'ready': '', 'indexing': ' [индексируется]', 'queued': ' [в очереди]',
                      'error': f' [ошибка: {r["error"]}]'}.get(r['status'], f' [{r["status"]}]')
            o(f"  {r['label']}  v{r['config_version'] or '?'}{ext}  индексирован {(r['indexed_at'] or '—')[:16]}  "
              f"объектов {st.get('objects', '—')}, модулей {st.get('modules', '—')}, методов {st.get('methods', '—')}"
              f"  профиль: {r['profile'] or 'универсальный'}{status}")
    return o.text()


def _type_filter(object_type):
    if not object_type:
        return None
    t = TYPE_ALIASES.get(object_type.strip().lower())
    if not t:
        raise NotFound(f'Неизвестный тип «{object_type}»',
                       difflib.get_close_matches(object_type.lower(), list(TYPE_ALIASES), n=5, cutoff=0.5))
    return t


def list_objects(conn, query=None, object_type=None, config=None, limit=50, offset=0) -> str:
    t = _type_filter(object_type)
    q = (query or '').strip().casefold()
    snaps = snapshots(conn, config)
    found: dict[str, list] = OrderedDict()
    for s in snaps:
        cat = catalog(conn, s)
        for nid, typ, name, syn in cat.objects:
            if t and typ != t:
                continue
            if q:
                n, sy = name.casefold(), syn.casefold()
                if n == q:
                    rank = 0
                elif n.startswith(q):
                    rank = 1
                elif q in n:
                    rank = 2
                elif q in sy:
                    rank = 3
                elif all(w in n or w in sy for w in q.split()):
                    rank = 4
                else:
                    continue
            else:
                rank = 0
            key = f'{typ}.{name}'
            if key not in found:
                found[key] = [rank, syn, []]
            found[key][2].append(s)
    items = sorted(found.items(), key=lambda kv: (kv[1][0], kv[0]))
    o = Out()
    total = len(items)
    if not total:
        hint = ''
        if q:
            names = [f'{typ}.{n}' for s in snaps for _, typ, n, _ in catalog(conn, s).objects]
            close = difflib.get_close_matches(query, names, n=6, cutoff=0.5)
            hint = ('. Возможно: ' + '; '.join(close)) if close else ''
        return f'Ничего не найдено{hint}\n'
    o(f'Найдено объектов: {total}' + (f' (показаны {offset + 1}–{min(total, offset + limit)})' if total > limit else ''))
    for key, (_, syn, ss) in items[offset:offset + limit]:
        where = '' if len(ss) == len(snaps) and len(snaps) > 1 else (' ' + tags(ss) if len(snaps) > 1 else '')
        o(f'{key}' + (f' — {syn}' if syn and syn != key.split(".", 1)[1] else '') + where)
    if len(snaps) > 1:
        o(f'(без пометки — есть во всех {len(snaps)} снимках)')
    if total > offset + limit:
        o(f'… ещё {total - offset - limit}, offset={offset + limit}')
    return o.text()


def get_object(conn, full_name, config=None, section=None) -> str:
    """section: None — реквизиты с типами, табличные части списком имён; 'all' — всё с типами;
    имя табличной части — только она с типами."""
    res = _per_snapshot(conn, config, lambda s: resolve_node(conn, s, full_name))
    s, r = res[0]
    o = Out()
    if r.kind not in ('object', 'config'):
        return _describe_subnode(conn, s, r)
    row = conn.execute('SELECT n.*, c.parsed FROM nodes n LEFT JOIN contents c ON c.id=n.content_id WHERE n.id=?',
                       (r.id,)).fetchone()
    if r.kind == 'config':
        return list_configs(conn)
    ob = db.unjz(row['parsed'])
    same = [x for x, rr in res[1:] if conn.execute('SELECT content_id FROM nodes WHERE id=?', (rr.id,)).fetchone()[0]
            == row['content_id']]
    diff = [x for x, _ in res[1:] if x not in same]
    o(f"{ob['md_type']}.{ob['name']}" + (f" — «{ob['synonym']}»" if ob['synonym'] else '') + f' {s.tag}'
      + (' (заимствован в расширении)' if ob.get('adopted') else ''))
    if same:
        o(f'  такое же описание в: {tags(same)}')
    if diff:
        o(f'  ОТЛИЧАЕТСЯ в: {tags(diff)} (см. diff_configs)')
    if ob['comment']:
        o(f"Комментарий: {ob['comment']}")
    props = {k: v for k, v in ob['props'].items() if k not in ('RegisterRecords', 'Source', 'Handler', 'MethodName',
                                                                 'EventRu', 'Type', 'BasedOn')}
    if props:
        o('Свойства: ' + ', '.join(f'{k}={v}' for k, v in props.items()))
    p = ob['props']
    if 'Type' in p:
        o(f"Тип: {fmt_types(p['Type'])}")
    if ob['md_type'] == 'ПодпискаНаСобытие':
        o(f"Событие: {p.get('EventRu')} ({p.get('Event')}); обработчик: {'.'.join(p['Handler']) if p.get('Handler') else '—'}")
        o(f"Источники ({len(p.get('Source', []))}): {fmt_types(p.get('Source', [])[:40])}"
          + (' …' if len(p.get('Source', [])) > 40 else ''))
    if ob['md_type'] == 'РегламентноеЗадание' and p.get('MethodName'):
        o(f"Метод: {'.'.join(p['MethodName'])}")
    sec = (section or '').strip().casefold()
    only_ts = sec and sec != 'all'
    if only_ts and not any(t['name'].casefold() == sec for t in ob['tabular_sections']):
        raise NotFound(f"Табличная часть «{section}» не найдена", [t['name'] for t in ob['tabular_sections']] + ['all'])
    groups = defaultdict(list)
    for a in ob['attributes']:
        groups[a['kind']].append(a)
    for kind, title in (('Измерение', 'Измерения'), ('Ресурс', 'Ресурсы'), ('Реквизит', 'Реквизиты'),
                        ('РеквизитАдресации', 'Реквизиты адресации')):
        if groups.get(kind) and not only_ts:
            o(f'{title} ({len(groups[kind])}):')
            for a in groups[kind]:
                o(f"  {a['name']}: {fmt_types(a['types'])}")
    for ts in ob['tabular_sections']:
        if sec == 'all' or ts['name'].casefold() == sec:
            o(f"Табличная часть {ts['name']} ({len(ts['attributes'])}):")
            for a in ts['attributes']:
                o(f"  {a['name']}: {fmt_types(a['types'])}")
        elif not only_ts:
            o(f"Табличная часть {ts['name']} ({len(ts['attributes'])}): " + ', '.join(a['name'] for a in ts['attributes']))
    if only_ts:
        return o.text()
    if ob['tabular_sections'] and not sec:
        o('(типы реквизитов табличных частей: section=<имя ТЧ> или section=all)')
    if ob['enum_values']:
        o(f"Значения ({len(ob['enum_values'])}): " + ', '.join(v[0] for v in ob['enum_values']))
    if ob['forms']:
        o('Формы: ' + ', '.join(ob['forms']))
    if ob['commands']:
        o('Команды: ' + ', '.join(c[0] for c in ob['commands']))
    cat = catalog(conn, s)
    mods = cat.modules_of.get(r.id, [])
    if mods:
        cnt = {m: conn.execute("SELECT count(*) FROM nodes WHERE parent_id=? AND kind='method'", (m,)).fetchone()[0]
               for m, _ in mods}
        o('Модули: ' + ', '.join(f'{k} ({cnt[m]} методов)' for m, k in mods))
    if p.get('RegisterRecords'):
        o(f"Движения ({len(p['RegisterRecords'])}): " + ', '.join(p['RegisterRecords']))
    # edges around the object
    subs = conn.execute("SELECT to_id, attrs FROM edges WHERE from_id=? AND kind='event_subscription'", (r.id,)).fetchall()
    if subs:
        lab = labels(conn, [x[0] for x in subs])
        o(f'Подписки на события ({len(subs)}):')
        for to, a in sorted(subs, key=lambda x: _edge_attrs(x[1]).get('event', '')):
            a = _edge_attrs(a)
            o(f"  {a.get('event')}: {a.get('sub')} → {lab[to]}")
    pm = conn.execute("SELECT to_id, attrs FROM edges WHERE from_id=? AND kind='posting_mechanism'", (r.id,)).fetchall()
    if pm:
        lab = labels(conn, [x[0] for x in pm if x[0]])
        o('Механизмы проведения (эвристика по ПроведениеДокументов): ' + ', '.join(
            f"{_edge_attrs(a).get('mechanism') or _edge_attrs(a).get('step')} → {lab.get(t, '—')}" for t, a in pm))
    if ob['md_type'] in ('РегистрНакопления', 'РегистрСведений', 'РегистрБухгалтерии', 'РегистрРасчета'):
        docs = conn.execute("SELECT count(*) FROM edges WHERE to_id=? AND kind='register_records'", (r.id,)).fetchone()[0]
        w = conn.execute("SELECT count(*) FROM edges WHERE to_id=? AND kind='writes_register'", (r.id,)).fetchone()[0]
        rd = conn.execute("SELECT count(*) FROM edges WHERE to_id=? AND kind='reads_table'", (r.id,)).fetchone()[0]
        o(f'Движения делают документов: {docs}; пишут методов (по коду): {w}; читают методов: {rd} '
          f'(подробно: who_writes / who_reads)')
    refs = conn.execute("SELECT count(*) FROM edges WHERE to_id=? AND kind='attr_type'", (r.id,)).fetchone()[0]
    if refs:
        o(f'Ссылаются реквизитов (по типу): {refs} (подробно: get_dependencies direction=in edge_kinds=attr_type)')
    return o.text()


def _describe_subnode(conn, s: Snap, r) -> str:
    o = Out()
    o(f'{r.label} — {r.kind} {s.tag}')
    if r.kind == 'module':
        o(f'Методов: {conn.execute("SELECT count(*) FROM nodes WHERE parent_id=? AND kind=?", (r.id, "method")).fetchone()[0]}'
          ' (см. get_module_outline)')
    elif r.kind == 'method':
        cm = method_row(conn, r.id)
        o(f"{signature(conn, cm)}  стр. {cm['line_start']}–{cm['line_end']} (текст: get_procedure)")
    elif r.kind in ('attribute', 'tabular'):
        types = []
        for to, a in conn.execute("SELECT to_id, attrs FROM edges WHERE from_id=? AND kind='attr_type'", (r.id,)):
            via = _edge_attrs(a).get('via')
            types.append(labels(conn, [to])[to] + (f' (через {via})' if via else ''))
        if types:
            o('Ссылочные типы: ' + ', '.join(types))
        forms = conn.execute("SELECT count(*) FROM edges WHERE to_id=? AND kind='form_attr'", (r.id,)).fetchone()[0]
        o(f'Выводится на формах: {forms} (см. find_form_usages)')
    elif r.kind == 'form':
        items = conn.execute("SELECT count(*) FROM nodes WHERE parent_id=? AND kind='form_item'", (r.id,)).fetchone()[0]
        o(f'Элементов с обработчиками: {items}; обработчики: get_dependencies(direction=out, edge_kinds=form_handler)')
    return o.text()


# ------------------------------------------------------------------ code

def _fts_query(q: str) -> str:
    q = q.strip()
    if re.search(r'\b(AND|OR|NOT|NEAR)\b|"', q):
        return q
    words = re.findall(r'[\w]+\*?', q)
    if not words:
        raise NotFound('Пустой запрос')
    if len(words) == 1 and words[0].endswith('*'):
        return words[0]
    return '"' + ' '.join(w.rstrip('*') for w in words) + '"' + ('*' if words[-1].endswith('*') else '')


def search_code(conn, query, config=None, object_type=None, limit=20, offset=0) -> str:
    snaps = snapshots(conn, config)
    sids = {s.id: s for s in snaps}
    t = _type_filter(object_type)
    fq = _fts_query(query)
    words = [w.lower().rstrip('*') for w in re.findall(r'[\w]+\*?', query) if w.upper() not in ('AND', 'OR', 'NOT', 'NEAR')]
    phrase_re = re.compile(r'\W+'.join(re.escape(w) for w in words), re.I) if len(words) > 1 else None
    o = Out()
    hits = []
    seen = 0
    batch = 200
    pos = 0
    while len(hits) < offset + limit:
        rows = conn.execute('SELECT rowid FROM code_fts WHERE code_fts MATCH ? ORDER BY rank LIMIT ? OFFSET ?',
                            (fq, batch, pos)).fetchall()
        if not rows:
            break
        pos += batch
        cms = [r[0] for r in rows]
        where = {}
        qm = ",".join("?" * len(cms))
        for r in conn.execute(f"SELECT n.content_id, n.snapshot_id, p.full_name FROM nodes n JOIN nodes p ON "
                              f"p.id=n.parent_id WHERE n.kind='method' AND n.content_id IN ({qm})", cms):
            if r[1] in sids and (not t or r[2].startswith(t + '.')):
                where.setdefault(r[0], []).append((r[2], sids[r[1]]))
        for cm_id in cms:
            if cm_id in where:
                seen += 1
                hits.append((cm_id, where[cm_id]))
        if len(rows) < batch:
            break
    if not hits:
        return f'По запросу «{query}» ничего не найдено\n'
    o(f'Совпадения по «{query}»' + (f' (с {offset + 1})' if offset else '') + ':')
    for cm_id, places in hits[offset:offset + limit]:
        cm = conn.execute('SELECT * FROM content_methods WHERE id=?', (cm_id,)).fetchone()
        text = db.module_text(conn, cm['content_id'])
        body = text[cm['cstart']:cm['cend']]
        low = body.lower()
        m = phrase_re.search(body) if phrase_re else None
        idx = m.start() if m else min((low.find(w) for w in words if w and low.find(w) >= 0), default=0)
        line_no = text.count('\n', 0, cm['cstart'] + idx) + 1
        line = body[body.rfind('\n', 0, idx) + 1: (body.find('\n', idx) + 1 or len(body) + 1) - 1].strip()
        mods = OrderedDict()
        for full, s in places:
            mods.setdefault(short_module(full), []).append(s)
        for mod, ss in mods.items():
            o(f"{mod}.{cm['name']} стр. {line_no}: {line[:160]}" + (f'  {tags(ss)}' if len(snaps) > 1 else ''))
    more = len(hits) - offset - limit
    if more > 0:
        o(f'… есть ещё, offset={offset + limit}')
    return o.text()


def find_procedure(conn, name, config=None, module_hint=None, limit=30) -> str:
    parts = split_name(name)
    if not parts:
        raise NotFound('Пустое имя')
    key = parts[-1].lower()
    if len(parts) > 1 and not module_hint:
        module_hint = '.'.join(parts[:-1])
    snaps = snapshots(conn, config)
    sids = {s.id: s for s in snaps}
    hint = (module_hint or '').casefold().replace(' ', '')
    for stage, sql, arg in (('точное совпадение', 'cm.key = ?', key),
                            ('по началу имени', 'cm.key >= ? AND cm.key < ?', None),
                            ('по части имени', 'instr(cm.key, ?) > 0', key)):
        args = (key, key + '￿') if arg is None else (arg,)
        rows = conn.execute(f'SELECT cm.id FROM content_methods cm WHERE {sql} LIMIT 5000', args).fetchall()
        if not rows:
            continue
        ids = [r[0] for r in rows]
        found = OrderedDict()
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            for r in conn.execute("SELECT n.content_id, n.snapshot_id, p.full_name, p.parent_id FROM nodes n "
                                  "JOIN nodes p ON p.id=n.parent_id WHERE n.kind='method' AND n.content_id IN "
                                  f"({','.join('?' * len(part))})", part):
                if r[1] not in sids:
                    continue
                full = r[2]
                if hint:
                    cat = catalog(conn, sids[r[1]])
                    obj_full = '.'.join(full.split('.')[:2])
                    syn = cat.synonym.get(cat.by_full.get(obj_full.lower()), '')
                    if hint not in full.casefold().replace(' ', '') and hint not in syn.casefold().replace(' ', ''):
                        continue
                found.setdefault((r[0], full), []).append(sids[r[1]])
        if not found:
            continue
        o = Out()
        o(f'Найдено ({stage}): {len(found)}' + (f', показаны первые {limit}' if len(found) > limit else ''))
        for (cm_id, full), ss in list(found.items())[:limit]:
            cm = conn.execute('SELECT * FROM content_methods WHERE id=?', (cm_id,)).fetchone()
            o(f"{short_module(full)}: {signature(conn, cm)}  стр. {cm['line_start']}–{cm['line_end']}"
              + (f'  {tags(ss)}' if len(snaps) > 1 else ''))
        return o.text()
    # nothing: suggest close names
    close = [r[0] for r in conn.execute('SELECT DISTINCT name FROM content_methods WHERE key >= ? AND key < ? LIMIT 20',
                                        (key[:4], key[:4] + '￿'))]
    raise NotFound(f'Метод «{name}» не найден', difflib.get_close_matches(parts[-1], close, n=6, cutoff=0.5) or close[:6])


def _method_everywhere(conn, config, module, name):
    """[(snap, Resolved)] for a method (module may be None)."""
    return _per_snapshot(conn, config, lambda s: resolve_method(conn, s, module, name))


def get_procedure(conn, module, name, config=None, max_lines=300) -> str:
    res = _method_everywhere(conn, config, module, name)
    groups = OrderedDict()
    for s, r in res:
        cm = method_row(conn, r.id)
        groups.setdefault(cm['id'], [cm, r, []])[2].append(s)
    o = Out()
    (cm, r, ss), *others = groups.values()
    det = method_details(conn, cm)
    text = db.module_text(conn, cm['content_id']).split('\n')
    a, b = det.get('doc_line') or cm['line_start'], cm['line_end']
    o(f"{short_module(cm['module_full'])}.{cm['name']}  стр. {a}–{b}  {tags(ss)}")
    if det.get('regions'):
        o(f"Область: {' / '.join(det['regions'])}")
    if det.get('ext_kind'):
        o(f"Расширение: &{det['ext_kind']}(\"{det['ext_target']}\")")
    if others:
        o('В других снимках текст отличается: ' + '; '.join(tags(x[2]) for x in others) + ' (см. diff_procedure)')
    end = min(b, a + max_lines - 1)
    for i in range(a, end + 1):
        o(f'{i}: {text[i - 1].rstrip()}' if i - 1 < len(text) else f'{i}:')
    if end < b:
        o(f'… обрезано ({b - end} строк), продолжение: get_module(start_line={end + 1}, end_line={b})')
    return o.text()


def _module_everywhere(conn, config, module):
    return _per_snapshot(conn, config, lambda s: resolve_node(conn, s, module, want='module'))


def _nosrc_note(conn, res) -> str | None:
    """Message for a module shipped without source text (Module.bin), else None."""
    s, r = res[0]
    a = conn.execute('SELECT attrs FROM nodes WHERE id=?', (r.id,)).fetchone()[0]
    if a and json.loads(a).get('nosrc'):
        n = conn.execute("SELECT COUNT(*) FROM edges WHERE to_id=? AND confidence='unresolved'", (r.id,)).fetchone()[0]
        return (f'{r.label} {tags([x for x, _ in res])}: модуль поставляется без исходных текстов (Module.bin) — '
                f'текст и состав методов недоступны. Вызовов из кода: {n} (find_callers с module={r.label} '
                'и именем метода, или get_dependencies direction=in)')
    return None


def get_module_outline(conn, module, config=None, limit=150, offset=0) -> str:
    res = _module_everywhere(conn, config, module)
    note = _nosrc_note(conn, res)
    if note:
        return note
    groups = OrderedDict()
    for s, r in res:
        cid = conn.execute('SELECT content_id FROM nodes WHERE id=?', (r.id,)).fetchone()[0]
        groups.setdefault(cid, [r, []])[1].append(s)
    o = Out()
    (cid, (r, ss)), *others = groups.items()
    rows = conn.execute('SELECT * FROM content_methods WHERE content_id=? ORDER BY ord', (cid,)).fetchall()
    lines = conn.execute('SELECT lines FROM contents WHERE id=?', (cid,)).fetchone()[0]
    o(f'{r.label}: {len(rows)} методов, {lines} строк {tags(ss)}')
    if others:
        o('В других снимках модуль отличается: ' + '; '.join(tags(x[1][1]) for x in others) + ' (см. diff_module)')
    region = None
    for cm in rows[offset:offset + limit]:
        det = method_details(conn, cm)
        reg = ' / '.join(det.get('regions') or []) or None
        if reg != region:
            o(f'#Область {reg}' if reg else '(вне областей)')
            region = reg
        ext = f" &{det['ext_kind']}(\"{det['ext_target']}\")" if det.get('ext_kind') else ''
        o(f"  {cm['line_start']}–{cm['line_end']}  {signature(conn, cm)}{ext}")
    if len(rows) > offset + limit:
        o(f'… ещё {len(rows) - offset - limit}, offset={offset + limit}')
    return o.text()


def get_module(conn, module, config=None, start_line=None, end_line=None, max_lines=400) -> str:
    res = _module_everywhere(conn, config, module)
    note = _nosrc_note(conn, res)
    if note:
        return note
    s, r = res[0]
    cid = conn.execute('SELECT content_id FROM nodes WHERE id=?', (r.id,)).fetchone()[0]
    text = db.module_text(conn, cid).split('\n')
    n = len(text)
    a = max(1, int(start_line or 1))
    b = min(n, int(end_line or n), a + max_lines - 1)
    others = [x for x, rr in res[1:] if conn.execute('SELECT content_id FROM nodes WHERE id=?', (rr.id,)).fetchone()[0] != cid]
    same = [x for x, rr in res[1:] if x not in others]
    o = Out()
    o(f'{r.label} строки {a}–{b} из {n} {tags([s] + same)}')
    if others:
        o('В других снимках модуль отличается: ' + tags(others))
    for i in range(a, b + 1):
        o(f'{i}: {text[i - 1].rstrip()}')
    if b < n and (end_line is None or b < int(end_line)):
        o(f'… продолжение: start_line={b + 1}')
    return o.text()


# ------------------------------------------------------------------ graph

def _edge_line(lab, to, kind, conf, line, attrs, arrow='→') -> str:
    a = _edge_attrs(attrs)
    target = lab.get(to) if to else (a.get('target') or a.get('method') or '(не определено)')
    if arrow == '→' and to and a.get('method') and not target.casefold().endswith('.' + a['method'].casefold()):
        target += '.' + a['method']         # method of a module without source text
    extra = []
    for k in ('event', 'sub', 'mechanism', 'step', 'table', 'tabular', 'via', 'type', 'call_type', 'reason'):
        if a.get(k):
            extra.append(f'{k}={a[k]}')
    if a.get('elements'):
        extra.append('элементы: ' + ', '.join(a['elements'][:5]))
    if a.get('n'):
        extra.append(f"×{a['n']}")
    return (f"{arrow} [{kind}{_conf_mark(conf, a)}] {target}" + (f' (стр. {line})' if line else '')
            + (f"  {'; '.join(extra)}" if extra else ''))


def get_dependencies(conn, node, direction='out', edge_kinds=None, depth=1, config=None, limit=200) -> str:
    kinds = _kinds(edge_kinds)
    depth = max(1, min(5, int(depth or 1)))
    if direction not in ('out', 'in', 'both'):
        raise NotFound('direction: out | in | both')
    o = Out()
    for s, r in _per_snapshot(conn, config, lambda s: resolve_node(conn, s, node)):
        o(f'{r.label} ({r.kind}) {s.tag}')
        dirs = ['out', 'in'] if direction == 'both' else [direction]
        for d in dirs:
            reach = graph.reach(conn, [r.id], d, kinds, depth, ('exact', 'heuristic', 'unresolved'), limit=50000)
            edges = graph.tree_edges(conn, reach, d, kinds, ('exact', 'heuristic', 'unresolved', 'broken'))
            # unresolved/broken edges without a target (only for outgoing)
            loose = []
            if d == 'out':
                ids = [i for i, dd in reach.items() if dd < depth]
                for i in range(0, len(ids), 500):
                    part = ids[i:i + 500]
                    loose += [dict(x) for x in conn.execute(
                        f'SELECT from_id, to_id, kind, confidence, line, attrs FROM edges WHERE to_id IS NULL AND '
                        f'from_id IN ({",".join("?" * len(part))}) AND kind IN ({",".join("?" * len(kinds))})',
                        [*part, *kinds])]
            allr = edges + loose
            lab = labels(conn, [e['from_id'] for e in allr] + [e['to_id'] for e in allr])
            cnt = defaultdict(int)
            for e in allr:
                cnt[f"{e['kind']}:{e['confidence']}"] += 1
            o(('Исходящие' if d == 'out' else 'Входящие') + f' (глубина {depth}): узлов {len(reach) - 1}, связей '
              f'{len(allr)} — ' + ', '.join(f'{k} {v}' for k, v in sorted(cnt.items())))
            shown = 0
            key = 'from_id' if d == 'out' else 'to_id'
            other = 'to_id' if d == 'out' else 'from_id'
            for e in sorted(allr, key=lambda e: (reach.get(e[key], 0), e['kind'], lab.get(e[other]) or '')):
                if shown >= limit:
                    o(f'… ещё {len(allr) - shown} связей (уменьшите depth или задайте edge_kinds)')
                    break
                lvl = reach.get(e[key], 0)
                prefix = '  ' * (lvl + 1) + (f'{lab[e[key]]} ' if lvl else '')
                o(prefix + _edge_line(lab, e[other], e['kind'], e['confidence'], e['line'], e['attrs'],
                                      '→' if d == 'out' else '←'))
                shown += 1
        o()
    return o.text()


def _targets(conn, config, name, module):
    """Methods matching name (in module if given) per snapshot."""
    if module:
        return [(s, [r]) for s, r in _method_everywhere(conn, config, module, name)]
    parts = split_name(name)
    if len(parts) > 1:
        return [(s, [r]) for s, r in _method_everywhere(conn, config, '.'.join(parts[:-1]), parts[-1])]
    key = parts[-1].lower()
    out = []
    for s in snapshots(conn, config):
        ids = [x[0] for x in conn.execute(
            "SELECT n.id FROM content_methods cm JOIN nodes n ON n.content_id=cm.id AND n.kind='method' "
            'WHERE cm.key=? AND n.snapshot_id=?', (key, s.id))]
        if ids:
            lab = labels(conn, ids)
            from .query import Resolved
            out.append((s, [Resolved(i, 'method', lab[i]) for i in ids]))
    if not out:
        find_procedure(conn, name, config)      # raises NotFound with suggestions
    return out


def _nosrc_callers(conn, config, name, module, limit) -> str | None:
    """Callers of a method of a module without source text (edges to the module node, attrs.method)."""
    parts = split_name(name)
    if not module and len(parts) > 1:
        module, name = '.'.join(parts[:-1]), parts[-1]
    if not module:
        return None
    try:
        res = _module_everywhere(conn, config, module)
    except NotFound:
        return None
    if not _nosrc_note(conn, res):
        return None
    o = Out()
    for s, r in res:
        rows = [x for x in conn.execute("SELECT from_id, to_id, kind, confidence, line, attrs FROM edges "
                                        "WHERE to_id=? AND confidence='unresolved'", (r.id,))
                if (_edge_attrs(x['attrs']).get('method') or '').casefold() == name.casefold()]
        lab = labels(conn, [x['from_id'] for x in rows])
        o(f'{r.label}.{name} {s.tag}: модуль без исходных текстов, вызывающих {len(rows)}')
        for x in sorted(rows, key=lambda x: lab.get(x['from_id'], ''))[:limit]:
            o('  ' + _edge_line(lab, x['from_id'], x['kind'], x['confidence'], x['line'], x['attrs'], '←'))
        if len(rows) > limit:
            o(f'  … ещё {len(rows) - limit}')
    return o.text()


def find_callers(conn, name, module=None, config=None, depth=1, limit=100) -> str:
    depth = max(1, min(5, int(depth or 1)))
    nosrc = _nosrc_callers(conn, config, name, module, limit)
    if nosrc is not None:
        return nosrc
    o = Out()
    for s, targets in _targets(conn, config, name, module):
        if len(targets) > 1:
            o(f'{s.tag} Метод «{name}» есть в {len(targets)} модулях: ' + ', '.join(t.label for t in targets[:10])
              + (' …' if len(targets) > 10 else '') + ' — показаны вызовы всех; уточните module при необходимости')
        for t in targets:
            rows = conn.execute(f'SELECT from_id, to_id, kind, confidence, line, attrs FROM edges WHERE to_id=? AND kind IN '
                                f'({",".join("?" * len(CALLER_KINDS))})', (t.id, *CALLER_KINDS)).fetchall()
            ext = [x for x in rows if x['kind'] == 'extension_intercepts']
            lab = labels(conn, [x['from_id'] for x in rows])
            o(f'{t.label} {s.tag}: вызывающих {len(rows)}')
            cnt = defaultdict(int)
            for x in rows:
                cnt[x['confidence']] += 1
            if rows:
                o('  ' + ', '.join(f'{k}: {v}' for k, v in cnt.items()))
            for x in sorted(rows, key=lambda x: (x['kind'] != 'calls', lab.get(x['from_id'], '')))[:limit]:
                o('  ' + _edge_line(lab, x['from_id'], x['kind'], x['confidence'], x['line'], x['attrs'], '←'))
            if len(rows) > limit:
                o(f'  … ещё {len(rows) - limit}')
            if ext:
                o('  (есть перехваты в расширениях — см. строки extension_intercepts)')
            if depth > 1 and rows:
                reach = graph.reach(conn, [t.id], 'in', ['calls'], depth, ('exact', 'heuristic'), limit=50000)
                by_d = defaultdict(int)
                for d in reach.values():
                    by_d[d] += 1
                o('  Цепочки вызовов вверх: ' + ', '.join(f'уровень {d}: {n}' for d, n in sorted(by_d.items()) if d))
                top = [i for i, d in reach.items() if d == depth][:30]
                lab2 = labels(conn, top)
                if top:
                    o(f'  На уровне {depth}: ' + ', '.join(lab2[i] for i in top) + (' …' if by_d[depth] > 30 else ''))
    return o.text()


def find_calls(conn, module, name, config=None) -> str:
    o = Out()
    for s, r in _method_everywhere(conn, config, module, name):
        rows = conn.execute('SELECT from_id, to_id, kind, confidence, line, attrs FROM edges WHERE from_id=? '
                            'ORDER BY line', (r.id,)).fetchall()
        lab = labels(conn, [x['to_id'] for x in rows])
        cnt = defaultdict(int)
        for x in rows:
            cnt[f"{x['kind']}:{x['confidence']}"] += 1
        o(f'{r.label} {s.tag}: связей {len(rows)} — ' + ', '.join(f'{k} {v}' for k, v in sorted(cnt.items())))
        for x in rows:
            o('  ' + _edge_line(lab, x['to_id'], x['kind'], x['confidence'], x['line'], x['attrs']))
    return o.text()


def _snap_profile(conn, s: Snap):
    row = conn.execute('SELECT profile FROM snapshots WHERE id=?', (s.id,)).fetchone()
    return row[0] if row else None


def _docs_confirmed_by_code(conn, docs, writers) -> set:
    """Documents whose write to the register is also visible in code: a writer method (or a method
    calling it within 2 levels) is in the document's own modules or is reached via a posting mechanism."""
    if not docs or not writers:
        return set()
    near = set(graph.reach(conn, writers, 'in', ['calls'], 2, ('exact', 'heuristic'), limit=20000))
    near.update(writers)
    ids = list(near)
    out = set()
    docset = set(docs)
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        q = ",".join("?" * len(part))
        for (o_,) in conn.execute(f"SELECT p.parent_id FROM nodes n JOIN nodes p ON p.id = n.parent_id "
                                  f"WHERE n.id IN ({q})", part):
            if o_ in docset:
                out.add(o_)
        for (d,) in conn.execute(f"SELECT from_id FROM edges WHERE kind='posting_mechanism' AND to_id IN ({q})", part):
            if d in docset:
                out.add(d)
    return out


def who_writes(conn, register, config=None, limit=100) -> str:
    o = Out()
    for s, r in _per_snapshot(conn, config, lambda s: resolve_node(conn, s, register, want='object')):
        docs = [x[0] for x in conn.execute("SELECT from_id FROM edges WHERE to_id=? AND kind='register_records'", (r.id,))]
        w = conn.execute("SELECT from_id, confidence, line, attrs FROM edges WHERE to_id=? AND kind='writes_register'",
                         (r.id,)).fetchall()
        lab = labels(conn, docs + [x[0] for x in w])
        o(f'{r.label} {s.tag}')
        o(profiles.describe(_snap_profile(conn, s)))
        confirmed = _docs_confirmed_by_code(conn, docs, [x[0] for x in w if not _edge_attrs(x[3]).get('role')])
        o(f'Документы с движениями по регистру (метаданные RegisterRecords, точно): {len(docs)}; '
          f'запись найдена и в коде: {len(confirmed)}, только по RegisterRecords: {len(docs) - len(confirmed)}')
        for d in sorted(docs, key=lambda x: lab[x])[:limit]:
            o(f'  {lab[d]}' + ('' if d in confirmed else '  [только RegisterRecords]'))
        if len(docs) > limit:
            o(f'  … ещё {len(docs) - limit}')
        o(f'Методы, которые пишут регистр или готовят для него таблицу движений (по коду): {len(w)}'
          + (f' — exact {sum(1 for x in w if x[1] == "exact")}, ~эвристика '
             f'{sum(1 for x in w if x[1] != "exact")}' if w else ''))
        # exact (modules of documents with the register in RegisterRecords) first, then heuristic
        w = sorted(w, key=lambda x: (x[1] != 'exact', lab.get(x[0], '')))
        if len(w) > limit:
            by_mod = defaultdict(int)
            for x in w:
                by_mod[lab[x[0]].rsplit('.', 1)[0]] += 1
            o('  по модулям: ' + ', '.join(f'{short_module(m)} {n}' for m, n in
                                          sorted(by_mod.items(), key=lambda x: -x[1])[:15])
              + (f' … (модулей {len(by_mod)})' if len(by_mod) > 15 else ''))
        mech = []
        for i, (frm, conf, line, a) in enumerate(w):
            a = _edge_attrs(a)
            if i < limit:
                o(f"  {lab[frm]} (стр. {line}){_conf_mark(conf)}" + (f" через {a['via']}" if a.get('via') else '')
                  + (f" [{a['role']}]" if a.get('role') else ''))
            elif i == limit:
                o(f'  … ещё {len(w) - limit} (увеличьте limit)')
            pm = conn.execute("SELECT from_id, attrs FROM edges WHERE to_id=? AND kind='posting_mechanism'",
                              (frm,)).fetchall()
            if pm:
                mech.append((frm, pm))
        if mech:
            o('Механизмы проведения (профиль, эвристика): документ → механизм → метод, пишущий регистр')
            for frm, pm in mech:
                dl = labels(conn, [x[0] for x in pm])
                names = sorted(dl.values())
                mname = _edge_attrs(pm[0][1]).get('mechanism', '')
                in_rr = sum(1 for x in pm if x[0] in docs)
                o(f'  {lab[frm]} [механизм {mname}]: документов {len(names)}, из них с регистром в RegisterRecords '
                  f'{in_rr}: ' + ', '.join(n.split('.', 1)[1] for n in names[:15]) + (' …' if len(names) > 15 else ''))
        o()
    return o.text()


def who_reads(conn, object_or_register, config=None, limit=100, source=None) -> str:
    src = (source or '').strip().lower()
    if src not in ('', 'code', 'dcs', 'код', 'скд'):
        raise NotFound('source: code (только код) | dcs (только схемы СКД) | пусто — всё')
    o = Out()
    for s, r in _per_snapshot(conn, config, lambda s: resolve_node(conn, s, object_or_register, want='object')):
        rows = conn.execute("SELECT from_id, line, attrs FROM edges WHERE to_id=? AND kind='reads_table'",
                            (r.id,)).fetchall()
        lab = labels(conn, [x[0] for x in rows])
        ids = [x[0] for x in rows]
        dcs = set()
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            dcs.update(x[0] for x in conn.execute(
                f'SELECT n.id FROM nodes n JOIN content_methods cm ON cm.id = n.content_id '
                f'WHERE n.id IN ({",".join("?" * len(part))}) AND cm.flags & {FLAG_DATASET}', part))
        o(f'{r.label} {s.tag}: читают в запросах {len(rows)} (эвристика по текстам запросов) — '
          f'методы в коде {len(rows) - len(dcs)}, наборы данных схем СКД (отчёты и др.) {len(dcs)}')
        tables = defaultdict(int)
        for _, _, a in rows:
            a = _edge_attrs(a)
            tables[a.get('table') or (('ТЧ ' + a['tabular']) if a.get('tabular') else 'основная таблица')] += 1
        if tables:
            o('  по таблицам: ' + ', '.join(f'{k} {v}' for k, v in sorted(tables.items(), key=lambda x: -x[1])))
        if src in ('code', 'код'):
            rows = [x for x in rows if x[0] not in dcs]
        elif src in ('dcs', 'скд'):
            rows = [x for x in rows if x[0] in dcs]
        for frm, line, a in sorted(rows, key=lambda x: lab.get(x[0], ''))[:limit]:
            a = _edge_attrs(a)
            t = a.get('table') or (('ТЧ ' + a['tabular']) if a.get('tabular') else '')
            o(f'  {lab[frm]} (стр. {line})' + (f' [{t}]' if t else '') + (' [СКД]' if frm in dcs else ''))
        if len(rows) > limit:
            o(f'  … ещё {len(rows) - limit}')
        refs = conn.execute("SELECT count(*) FROM edges WHERE to_id=? AND kind='attr_type'", (r.id,)).fetchone()[0]
        if refs:
            o(f'  (ещё {refs} реквизитов ссылаются на этот тип — get_dependencies direction=in edge_kinds=attr_type)')
        o()
    return o.text()


WRITE_STEPS = [('ОбработкаПроверкиЗаполнения', 'проверка заполнения (при интерактивной записи / ПроверитьЗаполнение())'),
               ('ПередЗаписью', 'перед записью'), ('ПриЗаписи', 'при записи')]
POST_STEPS = WRITE_STEPS + [('ОбработкаПроведения', 'проведение')]


def on_write_or_post(conn, document, event='post', depth=2, config=None, verbose=False) -> str:
    depth = max(0, min(5, int(depth if depth is not None else 2)))
    limit = 30 if verbose else 6
    ev = (event or 'post').strip().lower()
    if ev in ('post', 'проведение', 'провести', 'posting'):
        steps, mode = POST_STEPS, 'post'
        if ev == 'posting':
            steps = [s for s in POST_STEPS if s[0] == 'ОбработкаПроведения']
    elif ev in ('write', 'запись', 'записать'):
        steps, mode = WRITE_STEPS, 'write'
    else:
        e = event_ru(event)
        steps = [s for s in POST_STEPS if s[0].lower() == e.lower()]
        mode = 'post' if e == 'ОбработкаПроведения' else 'event'
        if not steps:
            raise NotFound(f'Событие «{event}» не поддерживается', ['post', 'write'] + [s[0] for s in POST_STEPS])
    o = Out()
    for s, r in _per_snapshot(conn, config, lambda s: resolve_node(conn, s, document, want='object')):
        cat = catalog(conn, s)
        o(f'{r.label} {s.tag}: что выполняется при {"проведении" if mode == "post" else "записи"} (порядок платформы)')
        o(profiles.describe(_snap_profile(conn, s)))
        written: set = set()
        om = next((m for m, k in cat.modules_of.get(r.id, []) if k == 'МодульОбъекта'), None)
        n = 0
        for evname, title in steps:
            n += 1
            o(f'{n}. {evname} — {title}')
            handlers = []
            if om:
                mid = conn.execute("SELECT n.id FROM nodes n JOIN content_methods cm ON cm.id=n.content_id WHERE "
                                   "n.parent_id=? AND n.kind='method' AND cm.key=?", (om, evname.lower())).fetchone()
                if mid:
                    handlers.append((mid[0], 'модуль объекта'))
            for to, a in conn.execute("SELECT to_id, attrs FROM edges WHERE from_id=? AND kind='event_subscription'",
                                      (r.id,)):
                a = _edge_attrs(a)
                if a.get('event') == evname:
                    handlers.append((to, f"подписка {a.get('sub')}"))
            if evname == 'ОбработкаПроведения':
                for to, a in conn.execute("SELECT to_id, attrs FROM edges WHERE from_id=? AND kind='posting_mechanism' "
                                          "AND to_id IS NOT NULL", (r.id,)):
                    a = _edge_attrs(a)
                    handlers.append((to, 'механизм проведения ' + (a.get('mechanism') or a.get('step') or '')
                                     + ' (эвристика)'))
            if not handlers:
                o('   (обработчиков нет)')
            rr = {x[0] for x in conn.execute("SELECT to_id FROM edges WHERE from_id=? AND kind='register_records'",
                                             (r.id,))}
            written |= _describe_handlers(conn, o, handlers, depth, limit, verbose, rr=rr if rr else None)
        if mode == 'post':
            regs = [x[0] for x in conn.execute("SELECT to_id FROM edges WHERE from_id=? AND kind='register_records'",
                                               (r.id,))]
            n += 1
            o(f'{n}. Запись движений (Движения.Записать): регистров {len(regs)} (RegisterRecords); '
              f'запись найдена в коде обработчиков выше: {len(set(regs) & written)}')
            rl = labels(conn, regs)
            only_rr = sorted(rl[x] for x in regs if x not in written)
            if only_rr:
                o(f'   только по RegisterRecords ({len(only_rr)}): ' + ', '.join(x.split('.', 1)[1] for x in only_rr[:25])
                  + (' …' if len(only_rr) > 25 else ''))
            by_sub = defaultdict(list)     # (handler, sub, event) -> registers
            own = []
            for reg in sorted(regs, key=lambda x: rl[x]):
                rs = next((m for m, k in cat.modules_of.get(reg, []) if k == 'МодульНабораЗаписей'), None)
                for evname in ('ПередЗаписью', 'ПриЗаписи'):
                    if rs:
                        mid = conn.execute("SELECT n.id FROM nodes n JOIN content_methods cm ON cm.id=n.content_id "
                                           "WHERE n.parent_id=? AND n.kind='method' AND cm.key=?",
                                           (rs, evname.lower())).fetchone()
                        if mid:
                            own.append((mid[0], f'модуль набора записей, {evname}'))
                    for to, a in conn.execute("SELECT to_id, attrs FROM edges WHERE from_id=? AND "
                                              "kind='event_subscription'", (reg,)):
                        a = _edge_attrs(a)
                        if a.get('event') == evname:
                            by_sub[(to, a.get('sub'), evname)].append(reg)
            if own:
                o(f'   Модули наборов записей ({len(own)}):')
                _describe_handlers(conn, o, own, 1 if verbose else 0, limit, verbose, indent='     ')
            if by_sub:
                hl = labels(conn, [k[0] for k in by_sub])
                o(f'   Подписки на запись наборов ({len(by_sub)}):')
                for (h, sub, evname), rr in sorted(by_sub.items(), key=lambda kv: -len(kv[1])):
                    which = f'все {len(regs)}' if len(rr) == len(regs) else (
                        f'{len(rr)}: ' + ', '.join(rl[x].split('.', 1)[1] for x in rr[:5]) + (' …' if len(rr) > 5 else ''))
                    o(f'     • {hl[h]} [подписка {sub}, {evname}] — регистров {which}')
        o()
        if not verbose:
            o('(кратко; подробные списки вызовов — verbose=true, конкретный метод — find_calls)')
    return o.text()


def _describe_handlers(conn, o, handlers, depth, limit, verbose=False, indent='   ', rr=None):
    """rr: register ids of the document's RegisterRecords — written registers outside it are only counted.
    Returns ids of registers written by the handlers' call chains."""
    written: set = set()
    lab = labels(conn, [h for h, _ in handlers])
    for h, how in handlers:
        line = f'{indent}• {lab[h]} [{how}]'
        exts = []
        for frm, a in conn.execute("SELECT from_id, attrs FROM edges WHERE to_id=? AND kind='extension_intercepts'", (h,)):
            sn = conn.execute('SELECT s.label FROM nodes n JOIN snapshots s ON s.id=n.snapshot_id WHERE n.id=?',
                              (frm,)).fetchone()
            exts.append(f"{indent}  перехват &{_edge_attrs(a).get('type')}: {labels(conn, [frm])[frm]} "
                        f"[расширение {sn[0] if sn else '?'}]")
        if depth > 0:
            reach = graph.reach(conn, [h], 'out', ['calls'], depth, ('exact', 'heuristic'), limit=20000)
            direct = [i for i, d in reach.items() if d == 1]
            regs, other = [], 0
            ids = list(reach)
            for k in range(0, len(ids), 500):
                part = ids[k:k + 500]
                for to, a in conn.execute("SELECT DISTINCT to_id, attrs FROM edges WHERE kind='writes_register' AND "
                                          f"from_id IN ({','.join('?' * len(part))})", part):
                    if _edge_attrs(a).get('role') or to in regs:
                        continue                  # preparation of movement tables, not a write
                    if rr is not None and to not in rr:
                        other += 1
                        continue
                    regs.append(to)
            line += f' — вызовов {len(direct)}, в цепочке до глубины {depth}: {len(reach) - 1}'
            show_calls = verbose or 'модуль объекта' in how
            dl = labels(conn, (direct[:limit] if show_calls else []) + regs[:limit * 3])
            o(line)
            if exts:
                o('\n'.join(exts))
            if show_calls and direct:
                o(f'{indent}  вызывает: ' + ', '.join(sorted(dl[i] for i in direct[:limit]))
                  + (f' … (+{len(direct) - limit})' if len(direct) > limit else ''))
            if regs:
                o(f'{indent}  пишет регистры (по коду): ' + ', '.join(sorted(dl[i] for i in regs[:limit * 3]))
                  + (' …' if len(regs) > limit * 3 else '')
                  + (f' (+{other} регистров не из движений этого документа)' if other else ''))
            written.update(regs)
        else:
            o(line)
            if exts:
                o('\n'.join(exts))
    return written


IMPACT_KINDS = {
    'method': ['calls', 'handler', 'event_subscription', 'form_handler', 'scheduled_job', 'extension_intercepts'],
    'attribute': ['form_attr'],
    'tabular': ['form_attr'],
    'object': ['attr_type', 'reads_table', 'writes_register', 'register_records', 'calls', 'form_handler',
               'event_subscription', 'handler', 'posting_mechanism'],
}


def impact(conn, node, depth=2, config=None, limit=60) -> str:
    depth = max(1, min(5, int(depth or 2)))
    o = Out()
    for s, r in _per_snapshot(conn, config, lambda s: resolve_node(conn, s, node)):
        kinds = IMPACT_KINDS.get(r.kind, list(graph.KINDS))
        start = [r.id]
        if r.kind == 'module':
            start = [x[0] for x in conn.execute("SELECT id FROM nodes WHERE parent_id=? AND kind='method'", (r.id,))]
            kinds = IMPACT_KINDS['method']
        reach = graph.reach(conn, start, 'in', kinds, depth, ('exact', 'heuristic', 'unresolved'), limit=100000)
        for x in start:
            reach.pop(x, None)
        kind_of = {}
        ids = list(reach)
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            for nid, k, t in conn.execute(f'SELECT id, kind, type FROM nodes WHERE id IN ({",".join("?" * len(part))})',
                                          part):
                kind_of[nid] = k if k != 'object' else f'object:{t}'
        o(f'{r.label} ({r.kind}) {s.tag}: затронуто узлов {len(reach)} (обратный обход до глубины {depth})')
        by = defaultdict(list)
        for i, d in reach.items():
            by[(d, kind_of.get(i, '?'))].append(i)
        lab = labels(conn, [i for v in by.values() for i in v[:limit]])
        for (d, k), v in sorted(by.items()):
            o(f'  уровень {d}, {k}: {len(v)} — ' + ', '.join(sorted(lab[i] for i in v[:limit]))
              + (' …' if len(v) > limit else ''))
        if r.kind in ('attribute', 'tabular'):
            name = r.label.split('.')[-1]
            o(f'  Использование в коде по имени не разрешается статически: search_code("{name}")')
        o()
    return o.text()


PATH_KINDS = ('calls', 'form_handler', 'event_subscription', 'scheduled_job', 'handler', 'posting_mechanism',
              'writes_register', 'extension_intercepts', 'register_records', 'reads_table')


def call_path(conn, from_, to, max_depth=6, config=None, edge_kinds=None, max_paths=5) -> str:
    """Shortest paths between two nodes. edge_kinds: default ['calls']; e.g. form_handler + calls +
    posting_mechanism + writes_register: «команда формы → обработчик → … → документ → механизм → регистр».
    posting_mechanism also enables the virtual step «метод модуля документа/его формы → документ»."""
    max_depth = max(1, min(12, int(max_depth or 6)))
    kinds = _kinds(edge_kinds) if edge_kinds else ['calls']
    owner_step = 'posting_mechanism' in kinds
    o = Out()
    found_any = False
    errors = []
    for s in snapshots(conn, config):
        try:
            a = resolve_node(conn, s, from_)
            b = resolve_node(conn, s, to)
        except NotFound as e:
            errors.append(e)
            continue
        found_any = True
        starts = [a.id]
        if a.kind == 'module':
            starts = [x[0] for x in conn.execute("SELECT id FROM nodes WHERE parent_id=? AND kind='method'", (a.id,))]
        paths = graph.find_paths(conn, starts, [b.id], kinds, max_depth, max_paths=max_paths, owner_step=owner_step)
        if not paths:
            o(f'{s.tag} Путь от {a.label} к {b.label} по связям {", ".join(kinds)} до глубины {max_depth} не найден'
              + ('' if len(kinds) > 1 else ' (кнопки/подписки/механизмы проведения — задайте edge_kinds, '
                                            'напр. ["form_handler","calls","posting_mechanism","writes_register"])'))
            continue
        o(f'{s.tag} Кратчайшие пути ({len(paths[0]) - 1} шагов) по связям {", ".join(kinds)}:')
        for p in paths:
            lab = labels(conn, [n for n, _, _ in p])
            text = lab[p[0][0]]
            for n, kind, c in p[1:]:
                k = '' if kind == 'calls' else f'[{kind}] '
                text += f' →{_conf_mark(c) if c else ""} {k}{lab.get(n, n)}'
            o('  ' + text)
    if not found_any and errors:
        raise errors[0]
    return o.text()


# ------------------------------------------------------------------ versions

def _method_text(conn, snap, module, name):
    r = resolve_method(conn, snap, module, name)
    cm = method_row(conn, r.id)
    det = method_details(conn, cm)
    text = db.module_text(conn, cm['content_id'])
    a = det.get('doc_line') or cm['line_start']
    return r, cm, a, text.split('\n')[a - 1:cm['line_end']]


def diff_procedure(conn, module, name, config_a, config_b, context=3) -> str:
    sa, sb = one_snapshot(conn, config_a, 'config_a'), one_snapshot(conn, config_b, 'config_b')
    ra, cma, la, ta = _method_text(conn, sa, module, name)
    rb, cmb, lb, tb = _method_text(conn, sb, module, name)
    if cma['id'] == cmb['id'] or ta == tb:
        return f'{ra.label}: тексты идентичны в {sa.tag} и {sb.tag}\n'
    o = Out()
    o(f'{ra.label}: {sa.tag} стр. {la}–{cma["line_end"]}  →  {sb.tag} стр. {lb}–{cmb["line_end"]}')
    for line in difflib.unified_diff([x.rstrip() for x in ta], [x.rstrip() for x in tb],
                                     fromfile=sa.tag, tofile=sb.tag, n=context, lineterm=''):
        m = re.match(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', line)
        if m:     # hunk header with module line numbers
            line = f'@@ -{int(m.group(1)) + la - 1},{m.group(2) or 1} +{int(m.group(3)) + lb - 1},{m.group(4) or 1} @@'
        o(line)
    return o.text()


def _module_methods(conn, snap, module):
    r = resolve_node(conn, snap, module, want='module')
    cid = conn.execute('SELECT content_id FROM nodes WHERE id=?', (r.id,)).fetchone()[0]
    text = db.module_text(conn, cid)
    ms = OrderedDict()
    for cm in conn.execute('SELECT * FROM content_methods WHERE content_id=? ORDER BY ord', (cid,)):
        body = text[cm['cstart']:cm['cend']]
        ms.setdefault(cm['key'], (cm['name'], cm['line_start'], cm['line_end'], body))
    return r, cid, ms


def diff_module(conn, module, config_a, config_b) -> str:
    sa, sb = one_snapshot(conn, config_a, 'config_a'), one_snapshot(conn, config_b, 'config_b')
    ra, ca, ma = _module_methods(conn, sa, module)
    rb, cb, mb = _module_methods(conn, sb, module)
    if ca == cb:
        return f'{ra.label}: модуль идентичен в {sa.tag} и {sb.tag}\n'
    added = [mb[k] for k in mb if k not in ma]
    removed = [ma[k] for k in ma if k not in mb]
    changed, ws = [], []
    for k in ma:
        if k in mb and ma[k][3] != mb[k][3]:
            (ws if ' '.join(ma[k][3].split()) == ' '.join(mb[k][3].split()) else changed).append((ma[k], mb[k]))
    o = Out()
    o(f'{ra.label}: {sa.tag} → {sb.tag}: добавлено {len(added)}, удалено {len(removed)}, изменено {len(changed)}'
      + (f', только пробелы/переносы {len(ws)}' if ws else '') + f', без изменений {len(ma) - len(removed) - len(changed) - len(ws)}')
    for title, lst in (('Добавлены', added), ('Удалены', removed)):
        if lst:
            o(f'{title}:')
            for name, a, b, _ in lst:
                o(f'  {name} (стр. {a}–{b})')
    if changed:
        o('Изменены (текст: diff_procedure):')
        for (n, a1, b1, t1), (_, a2, b2, t2) in changed:
            d = sum(1 for x in difflib.ndiff(t1.split('\n'), t2.split('\n')) if x[:1] in '+-')
            o(f'  {n}: стр. {a1}–{b1} → {a2}–{b2}, строк изменено ~{d}')
    if ws:
        o('Только пробелы/переносы: ' + ', '.join(x[0][0] for x in ws))
    return o.text()


def diff_configs(conn, config_a, config_b, object_filter=None, limit=100, offset=0) -> str:
    sa, sb = one_snapshot(conn, config_a, 'config_a'), one_snapshot(conn, config_b, 'config_b')
    fa = {r[0]: r[1] for r in conn.execute('SELECT path, content_id FROM files WHERE snapshot_id=?', (sa.id,))}
    fb = {r[0]: r[1] for r in conn.execute('SELECT path, content_id FROM files WHERE snapshot_id=?', (sb.id,))}
    flt = (object_filter or '').strip()
    ftype = TYPE_ALIASES.get(flt.lower()) if flt else None
    fl = flt.casefold()

    def ok(name):
        if not flt:
            return True
        if ftype:
            return name.startswith(ftype + '.')
        return fl in name.casefold()

    def empty(cid):
        return not db.module_text(conn, cid).strip()

    entries = []
    empty_mods = []           # empty module files that appeared/disappeared (platform dump noise)
    layout_forms = 0          # Form.xml changed, but events/items/commands/elements are the same
    for path in sorted(set(fa) | set(fb)):
        e = classify(path)
        if e is None or e.kind in ('config', 'form_meta'):
            continue
        name = e.module_name if e.kind in ('module', 'template') else e.owner
        if not ok(name):
            continue
        a, b = fa.get(path), fb.get(path)
        if a == b:
            continue
        st = 'добавлен' if a is None else 'удалён' if b is None else 'изменён'
        if e.kind == 'object':
            detail = _object_diff(conn, a, b) if a and b else ''
            entries.append((0, f'{st}: {name}' + (f' — {detail}' if detail else '')))
        elif e.kind == 'module':
            if (a is None or b is None) and empty(a or b):
                empty_mods.append(f'{"+" if a is None else "-"}{short_module(name)}')
                continue
            entries.append((1, f'модуль {st}: {short_module(name)}' if not name.endswith('.Модуль') or '.Форма.' in name
                            or name.startswith(('ОбщийМодуль', 'ОбщаяФорма')) else f'модуль {st}: {name}'))
        elif e.kind == 'template':
            kinds = {r[0] for r in conn.execute('SELECT kind FROM contents WHERE id IN (?, ?)', (a or 0, b or 0))}
            if kinds == {'skip'}:
                continue        # spreadsheet/text templates are not indexed
            entries.append((1, f'схема СКД {st}: {name}'))
        elif e.kind == 'form':
            if a and b and _same_form(conn, a, b):
                layout_forms += 1
                continue
            entries.append((2, f'форма {st}: {name}'))
    entries.sort(key=lambda x: x[0])
    o = Out()
    counts = defaultdict(int)
    for k, _ in entries:
        counts[k] += 1
    o(f'{sa.tag} → {sb.tag}' + (f' (фильтр «{flt}»)' if flt else '') + f': объектов метаданных {counts[0]}, '
      f'модулей {counts[1]}, форм {counts[2]}')
    if layout_forms:
        o(f'  (форм, изменённых только в разметке — оформление, служебные подсказки: {layout_forms}; не считаются)')
    if empty_mods:
        o(f'  (пустые файлы модулей появились/исчезли: {len(empty_mods)} — не считаются: '
          + ', '.join(empty_mods[:8]) + (' …' if len(empty_mods) > 8 else '') + ')')
    for _, line in entries[offset:offset + limit]:
        o('  ' + line)
    if len(entries) > offset + limit:
        o(f'… ещё {len(entries) - offset - limit}, offset={offset + limit}')
    return o.text()


def _same_form(conn, ca, cb) -> bool:
    """Forms equal in what the index knows: events, items, commands, attributes and stored elements."""
    pa, pb = (conn.execute('SELECT parsed FROM contents WHERE id=?', (c,)).fetchone()[0] for c in (ca, cb))
    if db.unjz(pa) != db.unjz(pb):
        return False
    q = 'SELECT name, type, data_path, parent FROM form_elements WHERE content_id=? ORDER BY idx'
    return conn.execute(q, (ca,)).fetchall() == conn.execute(q, (cb,)).fetchall()


def _type_delta(ta: list, tb: list, show=4) -> str:
    """Short description of a type change: whole types for small ones, otherwise only added/removed parts
    (a composite type of 30 documents gaining one should not print 60 names)."""
    if len(ta) <= 2 and len(tb) <= 2:
        return f'{fmt_types(ta)} → {fmt_types(tb)}'
    plus = [t for t in tb if t not in ta]
    minus = [t for t in ta if t not in tb]

    def part(sign, lst):
        if not lst:
            return ''
        s = ', '.join(sign + fmt_types([t]) for t in lst[:show])
        return s + (f' … ещё {len(lst) - show}' if len(lst) > show else '')

    return ', '.join(x for x in (part('+', plus), part('−', minus)) if x) or 'порядок/квалификаторы'


def _object_diff(conn, ca, cb) -> str:
    a, b = db.unjz(conn.execute('SELECT parsed FROM contents WHERE id=?', (ca,)).fetchone()[0]), \
        db.unjz(conn.execute('SELECT parsed FROM contents WHERE id=?', (cb,)).fetchone()[0])
    out = []

    def attrs(o):
        d = {x['name']: x['types'] for x in o['attributes']}
        for t in o['tabular_sections']:
            d[t['name'] + '[ТЧ]'] = []
            for x in t['attributes']:
                d[f"{t['name']}.{x['name']}"] = x['types']
        return d

    da, db_ = attrs(a), attrs(b)
    plus = [k for k in db_ if k not in da]
    minus = [k for k in da if k not in db_]
    types = [k for k in da if k in db_ and da[k] != db_[k]]
    if plus:
        out.append('+' + ', +'.join(plus[:15]) + (' …' if len(plus) > 15 else ''))
    if minus:
        out.append('-' + ', -'.join(minus[:15]) + (' …' if len(minus) > 15 else ''))
    if types:
        out.append('тип изменён: ' + ', '.join(f'{k} ({_type_delta(da[k], db_[k])})' for k in types[:5])
                   + (f' … (ещё {len(types) - 5})' if len(types) > 5 else ''))
    for key, title in (('forms', 'формы'), ('commands', 'команды')):
        x, y = set(map(str, a[key])), set(map(str, b[key]))
        if x != y:
            out.append(f'{title}: ' + ' '.join([f'+{v}' for v in sorted(y - x)] + [f'-{v}' for v in sorted(x - y)]))
    ra, rb = set(a['props'].get('RegisterRecords', [])), set(b['props'].get('RegisterRecords', []))
    if ra != rb:
        out.append('движения: ' + ' '.join([f'+{v}' for v in sorted(rb - ra)] + [f'-{v}' for v in sorted(ra - rb)]))
    pa = {k: v for k, v in a['props'].items() if k != 'RegisterRecords'}
    pb = {k: v for k, v in b['props'].items() if k != 'RegisterRecords'}
    ch = [k for k in set(pa) | set(pb) if pa.get(k) != pb.get(k)]
    if ch:
        out.append('свойства: ' + ', '.join(sorted(ch)))
    if a['synonym'] != b['synonym']:
        out.append('синоним')
    return '; '.join(out) or 'прочие свойства'


# ------------------------------------------------------------------ quality / forms

def find_broken_refs(conn, config=None, kind=None, limit=100, offset=0) -> str:
    o = Out()
    for s in snapshots(conn, config):
        q = "SELECT from_id, kind, line, attrs FROM edges WHERE snapshot_id=? AND confidence='broken'"
        args = [s.id]
        if kind:
            q += ' AND kind=?'
            args.append(kind)
        rows = conn.execute(q, args).fetchall()
        cnt = defaultdict(int)
        optional = defaultdict(list)     # missing module -> rows (ОбщийМодуль("X") of an absent subsystem)
        listed = []
        for r in rows:
            a = _edge_attrs(r['attrs'])
            cnt[(r['kind'], a.get('reason') or ('динамический вызов' if a.get('dynamic') else 'нет цели'))] += 1
            if not kind and a.get('dynamic') and a.get('reason') == 'нет модуля':
                optional[(a.get('target') or '').rsplit('.', 1)[0]].append(r)
            else:
                listed.append((0 if not a.get('dynamic') else 1, r))
        o(f'{s.tag} битых ссылок: {len(rows)}' + ('' if not rows else ' — ' + ', '.join(
            f'{k}/{reason}: {v}' for (k, reason), v in sorted(cnt.items(), key=lambda x: -x[1]))))
        if optional:
            n_opt = sum(len(v) for v in optional.values())
            o(f'  Из них {n_opt} — ОбщийМодуль("X") на модули, которых нет в конфигурации ({len(optional)} модулей): '
              'обычно код библиотек для необязательных подсистем (под ПодсистемаСуществует). Свёрнуто, '
              'подробно — kind=calls. Больше всего: ' + ', '.join(
                  f'{m.removeprefix("ОбщийМодуль.")} ×{len(v)}'
                  for m, v in sorted(optional.items(), key=lambda x: -len(x[1]))[:12]))
        lab = labels(conn, list({r['from_id'] for _, r in listed}))
        rows = [r for _, r in sorted(listed, key=lambda x: (x[0], x[1]['kind'], lab.get(x[1]['from_id'], '')))]
        if listed and optional:
            o('  Остальные (сначала ссылки метаданных и прямые вызовы):')
        for r in rows[offset:offset + limit]:
            a = _edge_attrs(r['attrs'])
            o(f"  [{r['kind']}] {lab[r['from_id']]}" + (f" стр. {r['line']}" if r['line'] else '') +
              f" → {a.get('target')}" + (f" ({a['reason']})" if a.get('reason') else '') +
              (f" (динамически: {a['dynamic']})" if a.get('dynamic') else ''))
        if len(rows) > offset + limit:
            o(f'  … ещё {len(rows) - offset - limit}, offset={offset + limit}')
    return o.text()


def find_form_usages(conn, attribute, config=None) -> str:
    o = Out()
    for s, r in _per_snapshot(conn, config, lambda s: resolve_node(conn, s, attribute)):
        if r.kind not in ('attribute', 'tabular'):
            raise NotFound(f'«{attribute}» — не реквизит ({r.kind}). Пример: Документ.X.Контрагент или '
                           'Документ.X.Товары.Номенклатура')
        rows = conn.execute("SELECT from_id, attrs FROM edges WHERE to_id=? AND kind='form_attr'", (r.id,)).fetchall()
        lab = labels(conn, [x[0] for x in rows])
        o(f'{r.label} {s.tag}: выводится на формах {len(rows)}')
        for frm, a in sorted(rows, key=lambda x: lab[x[0]]):
            o(f"  {lab[frm]}: элементы {', '.join(_edge_attrs(a).get('elements', []))}")
        if not rows:
            o('  (связь строится только через основной реквизит формы: Объект.X / Запись.X)')
    return o.text()


def get_form_element(conn, form, element, config=None) -> str:
    o = Out()
    for s, r in _per_snapshot(conn, config, lambda s: resolve_node(conn, s, form)):
        fid = r.id
        if r.kind == 'form_item':
            fid = conn.execute('SELECT parent_id FROM nodes WHERE id=?', (r.id,)).fetchone()[0]
        row = conn.execute('SELECT kind, content_id, attrs, full_name FROM nodes WHERE id=?', (fid,)).fetchone()
        cid = row['content_id'] if row['kind'] == 'form' else _edge_attrs(row['attrs']).get('form_content')
        if not cid:
            raise NotFound(f'«{form}» — не форма')
        els = conn.execute('SELECT idx, name, type, data_path, parent FROM form_elements WHERE content_id=? ORDER BY idx',
                           (cid,)).fetchall()
        hit = [e for e in els if e['name'].casefold() == element.casefold()]
        if not hit:
            raise NotFound(f'{s.tag} Элемент «{element}» формы {row["full_name"]} не найден',
                           difflib.get_close_matches(element, [e['name'] for e in els], n=6, cutoff=0.5))
        e = hit[0]
        chain = []
        p = e['parent']
        while p is not None and p >= 0 and len(chain) < 20:
            chain.append(els[p]['name'])
            p = els[p]['parent']
        fp = parsed_of(conn, cid)
        o(f"{row['full_name']}.{e['name']} {s.tag}: {e['type']}")
        o(f"  Путь данных: {e['data_path'] or '—'}")
        if chain:
            o('  Родители: ' + ' ← '.join(chain))
        main = fp.get('main_attribute')
        if e['data_path'] and main and e['data_path'].startswith(main + '.'):
            for to, a in conn.execute("SELECT to_id, attrs FROM edges WHERE from_id=? AND kind='form_attr'", (fid,)):
                if e['name'] in _edge_attrs(a).get('elements', []):
                    o(f'  Реквизит объекта: {labels(conn, [to])[to]}')
                    break
            else:
                o(f"  Реквизит объекта: {e['data_path'][len(main) + 1:]} (стандартный или не найден в метаданных)")
        elif e['data_path']:
            o(f"  Связан с реквизитом формы «{e['data_path'].split('.')[0]}» (не основной реквизит)")
        item = conn.execute("SELECT id FROM nodes WHERE parent_id=? AND kind='form_item' AND key=?",
                            (fid, e['name'].lower())).fetchone()
        if item:
            rows = conn.execute("SELECT to_id, kind, confidence, line, attrs FROM edges WHERE from_id=?",
                                (item[0],)).fetchall()
            lab = labels(conn, [x[0] for x in rows])
            for x in rows:
                o('  ' + _edge_line(lab, x['to_id'], x['kind'], x['confidence'], x['line'], x['attrs']))
    return o.text()


# ------------------------------------------------------------------ service

def reindex(conn, config=None, full=False) -> str:
    if SERVICE is None:
        return 'Сервис индексации не запущен\n'
    return SERVICE.request_reindex(config, bool(full))


def index_status(conn) -> str:
    if SERVICE is None:
        return 'Сервис индексации не запущен\n'
    return SERVICE.status_text()
