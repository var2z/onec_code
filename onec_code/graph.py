"""Graph edges of a snapshot and graph traversals.

Edge kinds
  calls               method/module -> method      exact | heuristic | unresolved | broken
  handler             subscription/scheduled job -> method (the handler itself)
  event_subscription  source object -> handler method (attrs: event, sub)
  scheduled_job       scheduled job -> method
  form_handler        form / form item / form command -> method of the form module
  form_attr           form -> attribute / tabular section shown on it (via main attribute)
  attr_type           attribute -> object of its reference type (DefinedType expanded)
  register_records    document -> register (metadata RegisterRecords)
  writes_register     method -> register (Движения.X in code; profiles add literal register names
                      in posting helpers, e.g. ПроведениеДокументов.ОтразитьДвижения(Т, Д, "X"))
  reads_table         method -> object/register from query texts (heuristic)
  posting_mechanism   document -> method of a generic posting engine (profiles only, see profiles.py)
  extension_intercepts extension method -> method of the extended configuration

Confidence: exact | heuristic | unresolved | broken (link is known, target is missing).
Everything here follows platform rules; configuration-specific conventions live in profiles.py.
"""
from __future__ import annotations

import json
from collections import defaultdict

from .db import FLAG_EXPORT, unjz
from .layout import MANAGER_COLLECTIONS, REGISTER_TYPES
from .md_parser import expand_types, form_event_ru

SELF_REFS = {'этотобъект', 'этаформа', 'thisobject', 'thisform'}
VIRTUAL_TABLES = {
    'остатки', 'обороты', 'остаткииобороты', 'срезпоследних', 'срезпервых', 'движенияссубконто',
    'оборотыдтк', 'субконто', 'данныеграфика', 'фактическийпериоддействия', 'базарасчета', 'границы',
    'balance', 'turnovers', 'balanceandturnovers', 'slicelast', 'slicefirst', 'recordswithextdimensions',
    'drcrturnovers', 'extdimensions', 'scheduledata', 'actualactionperiod', 'base', 'boundaries',
}
KINDS = ('calls', 'handler', 'event_subscription', 'scheduled_job', 'form_handler', 'form_attr',
         'attr_type', 'register_records', 'writes_register', 'reads_table', 'posting_mechanism',
         'extension_intercepts')


class _Sink:
    """Collects edges, merging duplicates (same from/to/kind/confidence) with a counter."""

    def __init__(self, sid):
        self.sid = sid
        self.d: dict[tuple, list] = {}

    def add(self, frm, to, kind, conf, line=None, attrs=None, tkey=None):
        if frm is None:
            return
        # unresolved edges keep their target key (e.g. the method of a module without source)
        key = (frm, to, kind, conf, tkey if to is None or conf == 'unresolved' else None)
        cur = self.d.get(key)
        if cur is None:
            self.d[key] = [line, attrs, 1]
        else:
            cur[2] += 1

    def rows(self):
        for (frm, to, kind, conf, _), (line, attrs, n) in self.d.items():
            if n > 1:
                attrs = {**(attrs or {}), 'n': n}
            yield (self.sid, frm, to, kind, conf, line,
                   json.dumps(attrs, ensure_ascii=False, separators=(',', ':')) if attrs else None)


class _Ctx:
    def __init__(self, conn, sid):
        self.conn = conn
        self.sid = sid
        self.obj = {}                      # (type, key) -> id
        self.obj_by_type = defaultdict(list)
        self.by_full = {}                  # lower full name -> id
        self.child = {}                    # (parent, kind, key) -> id
        self.node_kind = {}
        self.node_content = {}
        self.node_attrs = {}
        self.module_owner = {}             # module id -> owner node id
        self.form_parent = {}              # form id -> owning object id
        for r in conn.execute('SELECT id, kind, type, key, full_name, parent_id, content_id, attrs FROM nodes '
                              "WHERE snapshot_id=? AND kind != 'method'", (sid,)):
            nid, kind = r['id'], r['kind']
            self.node_kind[nid] = kind
            self.node_content[nid] = r['content_id']
            if r['attrs']:
                self.node_attrs[nid] = json.loads(r['attrs'])
            if r['full_name']:
                self.by_full[r['full_name'].lower()] = nid
            if kind == 'object':
                self.obj[(r['type'], r['key'])] = nid
                self.obj_by_type[r['type']].append(nid)
            elif kind == 'module':
                self.module_owner[nid] = r['parent_id']
            elif r['parent_id'] is not None and r['key']:
                if kind == 'form':
                    self.form_parent[nid] = r['parent_id']
                self.child.setdefault((r['parent_id'], kind, r['key']), nid)
        # methods
        self.meth = defaultdict(dict)       # module id -> key -> (node id, flags)
        self.meth_ord = {}                  # (module id, ord) -> node id
        self.meth_key = {}                  # node id -> key
        self.meth_module = {}
        self.meth_pos = {}                  # node id -> ord
        for r in conn.execute('SELECT n.id, n.parent_id, cm.key, cm.flags, cm.ord FROM nodes n '
                              'JOIN content_methods cm ON cm.id = n.content_id '
                              "WHERE n.snapshot_id=? AND n.kind='method'", (sid,)):
            self.meth[r[1]].setdefault(r[2], (r[0], r[3]))
            self.meth_ord[(r[1], r[4])] = r[0]
            self.meth_key[r[0]] = r[2]
            self.meth_module[r[0]] = r[1]
            self.meth_pos[r[0]] = r[4]
        # object parse results
        self.blob = {}
        cids = {self.node_content[i]: i for i in self.obj.values() if self.node_content.get(i)}
        for cid, parsed in _fetch_parsed(conn, list(cids)):
            self.blob[cids[cid]] = unjz(parsed)
        # common modules
        self.common = {}                    # key -> module id
        self.global_meth = defaultdict(list)
        for (t, key), oid in self.obj.items():
            if t != 'ОбщийМодуль':
                continue
            mid = self.by_full.get(f'общиймодуль.{key}.модуль')
            if mid is None:
                continue
            self.common[key] = mid
            if self.blob.get(oid, {}).get('props', {}).get('Global'):
                for mk, (nid, flags) in self.meth[mid].items():
                    if flags & FLAG_EXPORT:
                        self.global_meth[mk].append(nid)
        self.registers = defaultdict(list)  # key -> [register object ids]
        for (t, key), oid in self.obj.items():
            if t in REGISTER_TYPES:
                self.registers[key].append(oid)
        defs = {self.blob[o]['name']: self.blob[o]['props'].get('Type', [])
                for o in self.obj_by_type.get('ОпределяемыйТип', []) if o in self.blob}
        chars = {self.blob[o]['name']: self.blob[o]['props'].get('Type', [])
                 for o in self.obj_by_type.get('ПланВидовХарактеристик', []) if o in self.blob}
        self.defined, self.chars = defs, chars
        # modules without source text (*.bin): their methods are unknown
        self.nosrc = {nid for nid, a in self.node_attrs.items() if a.get('nosrc')}

    def to_nosrc(self, sink, frm, kind, mid, method, line=None, attrs=None, tkey=None):
        """Edge to a module shipped without source: the target method cannot be checked."""
        sink.add(frm, mid, kind, 'unresolved', line,
                 {**(attrs or {}), 'method': method, 'reason': 'модуль без исходных текстов'},
                 tkey=tkey if tkey is not None else ('nosrc', method.lower()))

    def obj_by_full(self, full: str):
        t, _, n = full.partition('.')
        return self.obj.get((t, n.lower()))

    def type_targets(self, types, expand_any=True):
        """Compact types -> [(object id, via)] with DefinedType/Characteristic expansion.
        'any:Документ' (all documents) is expanded only when expand_any is set."""
        out = []
        for t in types:
            via = None
            if t.startswith(('def:', 'char:')):
                via = ('ОпределяемыйТип.' if t.startswith('def:') else 'Характеристика.') + t.split(':', 1)[1]
                exp = expand_types([t], self.defined, self.chars)
            else:
                exp = [t]
            for x in exp:
                if x.startswith(('ref:', 'obj:')):
                    oid = self.obj_by_full(x[4:])
                    if oid:
                        out.append((oid, via))
                elif x.startswith('any:') and expand_any:
                    out.extend((o, via or x) for o in self.obj_by_type.get(x[4:], []))
        return out

    def method_in(self, module_full: str, method: str):
        """('ОбщийМодуль.X', 'М') -> (module id, method node id | None)."""
        mid = self.by_full.get(module_full.lower() + '.модуль') or self.by_full.get(module_full.lower())
        if mid is None or self.node_kind.get(mid) != 'module':
            return None, None
        m = self.meth[mid].get(method.lower())
        return mid, (m[0] if m else None)


def _fetch_parsed(conn, cids):
    for i in range(0, len(cids), 500):
        part = cids[i:i + 500]
        yield from conn.execute(f'SELECT id, parsed FROM contents WHERE id IN ({",".join("?" * len(part))})', part)


# ------------------------------------------------------------------ build

def snapshot_profile(conn, sid: int, override: str | None = None):
    """Profile for a snapshot: by configuration name (extensions: by their base configuration)."""
    from . import profiles
    row = conn.execute('SELECT config_name, is_extension, base_snapshot_id, profile_override FROM snapshots '
                       'WHERE id=?', (sid,)).fetchone()
    if row is None:
        return None
    name = row['config_name']
    if row['is_extension']:
        base = find_base_snapshot(conn, sid)
        b = conn.execute('SELECT config_name FROM snapshots WHERE id=?', (base,)).fetchone() if base else None
        name = b[0] if b else ''
    return profiles.select(name, override if override is not None else row['profile_override'])


def build_edges(conn, sid: int, profile_override: str | None = None) -> dict:
    """Rebuild all edges of a snapshot. profile_override: None (snapshot setting / auto), 'none', code."""
    conn.execute('DELETE FROM edges WHERE snapshot_id=?', (sid,))
    ctx = _Ctx(conn, sid)
    ctx.profile = snapshot_profile(conn, sid, profile_override)
    sink = _Sink(sid)
    snap = conn.execute('SELECT is_extension FROM snapshots WHERE id=?', (sid,)).fetchone()
    parsed = _code_edges(ctx, sink, keep_all=bool(snap and snap[0]))
    _metadata_edges(ctx, sink)
    _form_edges(ctx, sink)
    if ctx.profile:
        ctx.profile.extra_edges(ctx, sink, parsed)
    if snap and snap[0]:
        _extension_edges(conn, ctx, sink, parsed)
    conn.executemany('INSERT INTO edges(snapshot_id, from_id, to_id, kind, confidence, line, attrs) '
                     'VALUES(?,?,?,?,?,?,?)', list(sink.rows()))
    conn.execute('UPDATE snapshots SET profile=? WHERE id=?', (ctx.profile.code if ctx.profile else None, sid))
    counts = defaultdict(int)
    for (frm, to, kind, conf, _), v in sink.d.items():
        counts[f'{kind}:{conf}'] += 1
    return dict(sorted(counts.items()))


def _code_edges(ctx: _Ctx, sink: _Sink, keep_all=False) -> dict:
    """Edges from code references, module by module (parse results are not all held in memory).
    Returns {module id: parse result} only for modules later passes need (the profile's modules;
    everything for extensions)."""
    mods = {nid: ctx.node_content[nid] for nid, k in ctx.node_kind.items() if k == 'module'}
    full_of = {v: k for k, v in ctx.by_full.items()}
    keep = {mid for mid in mods if keep_all or (ctx.profile and ctx.profile.keep_module(full_of.get(mid, '')))}
    by_cid = defaultdict(list)
    for mid, cid in mods.items():
        by_cid[cid].append(mid)
    kept = {}
    shadows = _shadow_names(ctx)
    for cid, blob in _fetch_parsed(ctx.conn, list(by_cid)):
        p = unjz(blob)
        for mid in by_cid[cid]:
            _module_code_edges(ctx, sink, mid, p, shadows)
            if mid in keep:
                kept[mid] = p
    return kept


def _shadow_names(ctx: _Ctx):
    """Form content id -> names of form attributes (they shadow common module names in form modules)."""
    form_cids = {}
    for nid, k in ctx.node_kind.items():
        if k == 'form' and ctx.node_content.get(nid):
            form_cids[nid] = ctx.node_content[nid]
        elif k == 'object' and ctx.node_attrs.get(nid, {}).get('form_content'):
            form_cids[nid] = ctx.node_attrs[nid]['form_content']
    form_attrs = {cid: {a.lower() for a in unjz(p).get('attributes', [])}
                  for cid, p in _fetch_parsed(ctx.conn, list(set(form_cids.values())))}
    return form_cids, form_attrs


def _module_code_edges(ctx: _Ctx, sink: _Sink, mid, p, shadows):
    # Names visible in a module that shadow common modules: attributes and tabular sections
    # of the owning object (object/record set modules), attributes of the form (form modules).
    form_cids, form_attrs = shadows
    owner = ctx.module_owner.get(mid)
    own_records = set()
    shadow = set()
    if owner is not None and ctx.node_kind.get(owner) == 'object':
        o = ctx.blob.get(owner, {})
        own_records = {x.lower() for x in o.get('props', {}).get('RegisterRecords', [])}
        shadow = {a['name'].lower() for a in o.get('attributes', [])} | \
                 {t['name'].lower() for t in o.get('tabular_sections', [])}
    if owner in form_cids:
        shadow |= form_attrs.get(form_cids[owner], set())
    for i, refs in enumerate(p['r']):
        frm = ctx.meth_ord.get((mid, i))
        for r in refs:
            _code_ref(ctx, sink, mid, frm, r, own_records, shadow)
    for r in p['b']:
        _code_ref(ctx, sink, mid, mid, r, own_records, shadow)


def _code_ref(ctx, sink, mid, frm, r, own_records, shadow=frozenset()):
    kind, target, line, detail = r
    own = ctx.meth[mid]
    if kind == 'call':
        parts = [x.lower() for x in target]
        n = len(parts)
        tos = []
        if n == 1:
            m = own.get(parts[0])
            if m:
                tos = [(m[0], 'exact')]
            else:
                g = ctx.global_meth.get(parts[0], [])
                tos = [(x, 'exact' if len(g) == 1 else 'heuristic') for x in g]
        elif n == 2:
            a, b = parts
            if a in SELF_REFS:
                m = own.get(b)
                if m:
                    tos = [(m[0], 'exact')]
            elif a in ctx.common and not (detail and detail.get('l')) and a not in shadow:
                m = ctx.meth[ctx.common[a]].get(b)
                if m:
                    tos = [(m[0], 'exact')]
                elif ctx.common[a] in ctx.nosrc:
                    ctx.to_nosrc(sink, frm, 'calls', ctx.common[a], target[1], line)
                else:
                    sink.add(frm, None, 'calls', 'broken', line,
                             {'target': f'ОбщийМодуль.{target[0]}.{target[1]}'}, tkey=(a, b))
        elif n == 3 and parts[0] in MANAGER_COLLECTIONS:
            t = MANAGER_COLLECTIONS[parts[0]]
            oid = ctx.obj.get((t, parts[1]))
            if oid is None:
                if t not in ('ОбщийМодуль',):
                    sink.add(frm, None, 'calls', 'broken', line,
                             {'target': f'{t}.{target[1]}.{target[2]}', 'reason': 'нет объекта'},
                             tkey=(t, parts[1], parts[2]))
            else:
                mm = ctx.by_full.get(f'{t}.{parts[1]}.модульменеджера'.lower())
                m = ctx.meth[mm].get(parts[2]) if mm else None
                if m:
                    tos = [(m[0], 'exact')]
                elif mm in ctx.nosrc:
                    ctx.to_nosrc(sink, frm, 'calls', mm, target[2], line)
        for to, conf in tos:
            sink.add(frm, to, 'calls', conf, line)
            if ctx.profile:
                ctx.profile.on_call(ctx, sink, frm, to, target, line, detail)
        return
    if kind == 'dyn':
        d = detail
        typ = d['type']
        if typ == 'common_module':
            mod, meth = d.get('module'), d.get('method')
            if mod is None:
                sink.add(frm, None, 'calls', 'unresolved', line,
                         {'dynamic': 'ОбщийМодуль', 'reason': d.get('reason'), 'method': meth},
                         tkey=('cm', meth))
                return
            if '.' in mod:      # БСП: ОбщийМодуль("Обработки.X") returns the manager module
                coll, _, oname = mod.partition('.')
                t = MANAGER_COLLECTIONS.get(coll.lower())
                cm = ctx.by_full.get(f'{t}.{oname}.модульменеджера'.lower()) if t else None
                what = f'{t}.{oname}.МодульМенеджера' if t else mod
            else:
                cm = ctx.common.get(mod.lower())
                what = f'ОбщийМодуль.{mod}'
            if cm is None:
                sink.add(frm, None, 'calls', 'broken', line,
                         {'dynamic': 'ОбщийМодуль', 'target': what + (f'.{meth}' if meth else ''),
                          'reason': 'нет модуля'}, tkey=(mod.lower(), meth))
                return
            if meth is None:
                return          # module obtained but not called here (passed on)
            m = ctx.meth[cm].get(meth.lower())
            if m:
                sink.add(frm, m[0], 'calls', 'heuristic', line, {'dynamic': 'ОбщийМодуль'})
            elif cm in ctx.nosrc:
                ctx.to_nosrc(sink, frm, 'calls', cm, meth, line, {'dynamic': 'ОбщийМодуль'})
            elif '.' in mod:
                return          # manager: platform methods (ПустаяСсылка, Создать, ПолучитьМакет...)
            else:
                sink.add(frm, None, 'calls', 'broken', line,
                         {'dynamic': 'ОбщийМодуль', 'target': f'{what}.{meth}', 'reason': 'нет метода'},
                         tkey=(mod.lower(), meth.lower()))
        elif typ == 'execute':
            sink.add(frm, None, 'calls', 'unresolved', line, {'dynamic': d['fn']}, tkey=('exec', line))
        elif typ == 'notify':
            meth, tgt = d.get('method'), (d.get('target') or '').lower()
            if meth is None:
                sink.add(frm, None, 'calls', 'unresolved', line, {'dynamic': 'ОписаниеОповещения'},
                         tkey=('notify', line))
                return
            mm = mid if tgt in SELF_REFS or tgt == '' else ctx.common.get(tgt)
            m = ctx.meth[mm].get(meth.lower()) if mm else None
            sink.add(frm, m[0] if m else None, 'calls', 'unresolved', line,
                     {'dynamic': 'ОписаниеОповещения', 'method': meth,
                      **({} if m else {'target': d.get('target')})}, tkey=('notify', meth.lower(), tgt))
        elif typ == 'string':
            cm = ctx.common.get(d['module'].lower())
            if cm is not None:
                m = ctx.meth[cm].get(d['method'].lower())
                if m:
                    sink.add(frm, m[0], 'calls', 'unresolved', line, {'dynamic': 'строка'})
        return
    if kind == 'writes':
        key = target[0].lower()
        regs = ctx.registers.get(key, [])
        if not regs:
            return
        exact = [o for o in regs if ctx.node_kind.get(o) and _full(ctx, o).lower() in own_records]
        if exact:
            for o in exact:
                sink.add(frm, o, 'writes_register', 'exact', line, {'via': 'Движения'})
        else:
            for o in regs:
                sink.add(frm, o, 'writes_register', 'heuristic', line, {'via': 'Движения'})
        return
    if kind == 'query':
        typ, name, sub = target
        oid = ctx.obj.get((typ, name.lower()))
        if oid is None:
            return
        attrs = None
        if sub:
            attrs = {'table': sub} if sub.lower() in VIRTUAL_TABLES else {'tabular': sub}
        sink.add(frm, oid, 'reads_table', 'heuristic', line, attrs)


def _full(ctx, oid):
    b = ctx.blob.get(oid)
    return f"{b['md_type']}.{b['name']}" if b else ''


def _metadata_edges(ctx: _Ctx, sink: _Sink):
    for oid, o in ctx.blob.items():
        t = o['md_type']
        props = o['props']
        # attribute types
        for a in o['attributes']:
            aid = ctx.child.get((oid, 'attribute', a['name'].lower()))
            for to, via in ctx.type_targets(a['types'], expand_any=False):
                sink.add(aid, to, 'attr_type', 'exact', None, {'via': via} if via else None)
        for ts in o['tabular_sections']:
            tid = ctx.child.get((oid, 'tabular', ts['name'].lower()))
            for a in ts['attributes']:
                aid = ctx.child.get((tid, 'attribute', a['name'].lower())) if tid else None
                for to, via in ctx.type_targets(a['types'], expand_any=False):
                    sink.add(aid, to, 'attr_type', 'exact', None, {'via': via} if via else None)
        if t == 'Документ':
            for reg in props.get('RegisterRecords', []):
                rid = ctx.obj_by_full(reg)
                if rid:
                    sink.add(oid, rid, 'register_records', 'exact')
                else:
                    sink.add(oid, None, 'register_records', 'broken', None, {'target': reg}, tkey=reg)
        elif t == 'ПодпискаНаСобытие':
            h = props.get('Handler')
            ev = props.get('EventRu') or props.get('Event')
            if not h:
                continue
            hmid, mth = ctx.method_in(h[0], h[1])
            if mth is None and hmid in ctx.nosrc:
                ctx.to_nosrc(sink, oid, 'handler', hmid, h[1], attrs={'event': ev})
                continue
            if mth is None:
                sink.add(oid, None, 'handler', 'broken', None, {'target': f'{h[0]}.{h[1]}', 'event': ev},
                         tkey='h')
                continue
            sink.add(oid, mth, 'handler', 'exact', None, {'event': ev})
            for src, via in ctx.type_targets(props.get('Source', [])):
                sink.add(src, mth, 'event_subscription', 'exact', None, {'event': ev, 'sub': o['name']})
        elif t == 'РегламентноеЗадание':
            h = props.get('MethodName')
            if not h:
                continue
            jmid, mth = ctx.method_in(h[0], h[1])
            if mth is None and jmid in ctx.nosrc:
                ctx.to_nosrc(sink, oid, 'scheduled_job', jmid, h[1])
            elif mth is None:
                sink.add(oid, None, 'scheduled_job', 'broken', None, {'target': f'{h[0]}.{h[1]}'}, tkey='j')
            else:
                sink.add(oid, mth, 'scheduled_job', 'exact')


def _form_edges(ctx: _Ctx, sink: _Sink):
    forms = []       # (form node id, full name, form content id)
    for nid, kind in ctx.node_kind.items():
        if kind == 'form' and ctx.node_content.get(nid):
            forms.append(nid)
        elif kind == 'object' and ctx.node_attrs.get(nid, {}).get('form_content'):
            forms.append(nid)
    full_of = {v: k for k, v in ctx.by_full.items()}
    fcid = {f: (ctx.node_content[f] if ctx.node_kind[f] == 'form' else ctx.node_attrs[f]['form_content'])
            for f in forms}
    blobs = {cid: unjz(p) for cid, p in _fetch_parsed(ctx.conn, list(set(fcid.values())))}
    elements = defaultdict(list)
    cids = list(set(fcid.values()))
    for i in range(0, len(cids), 500):
        part = cids[i:i + 500]
        for r in ctx.conn.execute('SELECT content_id, name, data_path FROM form_elements WHERE data_path IS NOT NULL '
                                  f'AND content_id IN ({",".join("?" * len(part))})', part):
            elements[r[0]].append((r[1], r[2]))
    for fid in forms:
        f = blobs.get(fcid[fid])
        if not f:
            continue
        full = full_of.get(fid, '')
        mid = ctx.by_full.get(full + '.модуль')
        methods = ctx.meth.get(mid, {}) if mid else {}

        def handler(frm, ev):
            m = methods.get(ev['handler'].lower())
            attrs = {'event': form_event_ru(ev['event']) if ev['event'] != 'Action' else 'Действие'}
            if ev.get('call_type'):
                attrs['call_type'] = ev['call_type']
            if m:
                sink.add(frm, m[0], 'form_handler', 'exact', None, attrs)
            elif mid in ctx.nosrc:
                ctx.to_nosrc(sink, frm, 'form_handler', mid, ev['handler'], attrs=attrs)
            else:
                sink.add(frm, None, 'form_handler', 'broken', None,
                         {**attrs, 'target': ev['handler'], 'reason': 'нет метода в модуле формы'},
                         tkey=(ev['event'], ev['handler']))

        for ev in f['events']:
            handler(fid, ev)
        for it in f['items']:
            iid = ctx.child.get((fid, 'form_item', it['name'].lower()))
            for ev in it['events']:
                handler(iid, ev)
        for c in f['commands']:
            cid = ctx.child.get((fid, 'form_command', c['name'].lower()))
            for ev in c['actions']:
                handler(cid, ev)
        # form -> attributes of the main attribute's object
        main = f.get('main_attribute')
        target_obj = None
        for t in f.get('main_attribute_types') or []:
            if t.startswith(('obj:', 'ref:')):
                target_obj = ctx.obj_by_full(t[4:])
                break
        if not main or target_obj is None:
            continue
        pref = main + '.'
        agg = defaultdict(list)
        for name, dp in elements.get(fcid[fid], ()):
            if not dp.startswith(pref):
                continue
            parts = dp[len(pref):].split('.')
            k0 = parts[0].lower()
            ts = ctx.child.get((target_obj, 'tabular', k0))
            if ts is not None:
                to = ctx.child.get((ts, 'attribute', parts[1].lower())) if len(parts) > 1 else ts
                to = to or ts
            else:
                to = ctx.child.get((target_obj, 'attribute', k0))
            if to is not None:
                agg[to].append(name)
        for to, names in agg.items():
            sink.add(fid, to, 'form_attr', 'exact', None, {'elements': names[:10]})


# ------------------------------------------------------------------ extensions

def find_base_snapshot(conn, ext_sid: int) -> int | None:
    """The non-extension snapshot containing most of the extension's adopted objects."""
    adopted = [r[0].lower() for r in conn.execute(
        "SELECT full_name FROM nodes WHERE snapshot_id=? AND kind='object' AND attrs LIKE '%\"adopted\":true%'",
        (ext_sid,))]
    if not adopted:
        return None
    best, best_n = None, 0
    for s in conn.execute("SELECT id FROM snapshots WHERE is_extension=0 AND status IN ('ready','indexing') "
                          'AND id != ? ORDER BY indexed_at DESC', (ext_sid,)):
        names = {r[0].lower() for r in conn.execute(
            "SELECT full_name FROM nodes WHERE snapshot_id=? AND kind='object'", (s[0],))}
        n = sum(1 for a in adopted if a in names)
        if n > best_n:
            best, best_n = s[0], n
    return best


def _extension_edges(conn, ctx: _Ctx, sink: _Sink, parsed: dict):
    base = find_base_snapshot(conn, ctx.sid)
    conn.execute('UPDATE snapshots SET base_snapshot_id=? WHERE id=?', (base, ctx.sid))
    bctx = _Ctx(conn, base) if base else None
    full_of = {v: k for k, v in ctx.by_full.items()}
    for mid, p in parsed.items():
        mfull = full_of.get(mid)
        for o, det in enumerate(p.get('m', [])):
            ext_kind, ext_target = det[4], det[5]
            if not ext_kind:
                continue
            frm = ctx.meth_ord.get((mid, o))
            attrs = {'type': ext_kind}
            if bctx is None:
                sink.add(frm, None, 'extension_intercepts', 'unresolved', None,
                         {**attrs, 'target': f'{mfull}.{ext_target}', 'reason': 'нет снимка основной конфигурации'},
                         tkey='nobase')
                continue
            bmid = bctx.by_full.get(mfull)
            m = bctx.meth.get(bmid, {}).get((ext_target or '').lower()) if bmid else None
            if m:
                sink.add(frm, m[0], 'extension_intercepts', 'exact', None, attrs)
            else:
                sink.add(frm, None, 'extension_intercepts', 'broken', None,
                         {**attrs, 'target': f'{mfull}.{ext_target}'}, tkey=ext_target)
    if bctx is None:
        return
    # form events with callType in extension forms intercept the base form's handler
    bfull_of = {v: k for k, v in bctx.by_full.items()}
    for fid, kind in ctx.node_kind.items():
        if kind != 'form' or not ctx.node_content.get(fid):
            continue
        full = full_of.get(fid)
        bfid = bctx.by_full.get(full)
        if not bfid or not bctx.node_content.get(bfid):
            continue
        f = next((unjz(x[1]) for x in _fetch_parsed(conn, [ctx.node_content[fid]])), None)
        bf = next((unjz(x[1]) for x in _fetch_parsed(conn, [bctx.node_content[bfid]])), None)
        if not f or not bf:
            continue
        bhandlers = {e['event']: e['handler'] for e in bf['events']}
        emod = ctx.by_full.get(full + '.модуль')
        bmod = bctx.by_full.get(bfull_of[bfid] + '.модуль')
        for e in f['events']:
            if not e.get('call_type') or e['event'] not in bhandlers:
                continue
            m = ctx.meth.get(emod, {}).get(e['handler'].lower())
            bm = bctx.meth.get(bmod, {}).get(bhandlers[e['event']].lower())
            if m and bm:
                sink.add(m[0], bm[0], 'extension_intercepts', 'exact', None,
                         {'type': e['call_type'], 'event': form_event_ru(e['event'])})


# ------------------------------------------------------------------ traversals

def reach(conn, start_ids, direction='out', kinds=None, max_depth=3, confidences=None, limit=20000):
    """Nodes reachable from start_ids within max_depth: {node id: min depth}.

    Recursive CTE over (id, depth) with UNION: every row is unique and depth is bounded,
    so cycles cannot loop."""
    import json as _json
    kinds = list(kinds or KINDS)
    conf = list(confidences or ('exact', 'heuristic', 'unresolved'))
    kf = f'e.kind IN ({",".join("?" * len(kinds))}) AND e.confidence IN ({",".join("?" * len(conf))})'
    parts = []
    params = [_json.dumps(list(start_ids))]
    if direction in ('out', 'both'):
        parts.append(f'SELECT e.to_id, w.d + 1 FROM w JOIN edges e ON e.from_id = w.id '
                     f'WHERE w.d < ? AND e.to_id IS NOT NULL AND {kf}')
        params += [max_depth, *kinds, *conf]
    if direction in ('in', 'both'):
        parts.append(f'SELECT e.from_id, w.d + 1 FROM w JOIN edges e ON e.to_id = w.id WHERE w.d < ? AND {kf}')
        params += [max_depth, *kinds, *conf]
    sql = ('WITH RECURSIVE w(id, d) AS (SELECT value, 0 FROM json_each(?) UNION '
           + ' UNION '.join(parts) + ' LIMIT ?) SELECT id, MIN(d) FROM w GROUP BY id')
    params.append(limit)
    return {r[0]: r[1] for r in conn.execute(sql, params)}


def tree_edges(conn, depths: dict, direction='out', kinds=None, confidences=None):
    """Edges connecting consecutive levels of a reach() result (BFS tree-like view)."""
    kinds = list(kinds or KINDS)
    conf = list(confidences or ('exact', 'heuristic', 'unresolved'))
    ids = [i for i, d in depths.items()]
    out = []
    col, other = ('from_id', 'to_id') if direction == 'out' else ('to_id', 'from_id')
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for r in conn.execute(
                f'SELECT from_id, to_id, kind, confidence, line, attrs FROM edges WHERE {col} IN '
                f'({",".join("?" * len(part))}) AND kind IN ({",".join("?" * len(kinds))}) '
                f'AND confidence IN ({",".join("?" * len(conf))})', [*part, *kinds, *conf]):
            a, b = r[col], r[other]
            if b is not None and depths.get(b) == depths[a] + 1:
                out.append(dict(r))
    return out


def find_paths(conn, from_ids, to_ids, kinds=('calls',), max_depth=6, confidences=None, max_paths=5,
               owner_step=False, max_nodes=300000):
    """Shortest paths over the given edge kinds (level-by-level BFS, all shortest parents kept).

    owner_step: virtual step from a method of a document's module (object/manager module or a
    module of its form) to the document itself — "the code writes/posts this document"; lets a
    path continue through posting_mechanism edges of the document.
    Returns [[(node, kind, confidence) ...]], the first element's kind/confidence are None."""
    conf = list(confidences or ('exact', 'heuristic', 'unresolved'))
    kinds = list(kinds)
    targets = set(to_ids)
    parents: dict[int, list] = {n: [] for n in from_ids}
    level = list(from_ids)
    found = [n for n in level if n in targets]
    depth = 0
    while level and not found and depth < max_depth and len(parents) < max_nodes:
        depth += 1
        nxt: dict[int, list] = {}
        for i in range(0, len(level), 500):
            part = level[i:i + 500]
            q = ','.join('?' * len(part))
            for frm, to, kind, c in conn.execute(
                    f'SELECT from_id, to_id, kind, confidence FROM edges WHERE from_id IN ({q}) AND to_id IS NOT NULL '
                    f'AND kind IN ({",".join("?" * len(kinds))}) AND confidence IN ({",".join("?" * len(conf))})',
                    [*part, *kinds, *conf]):
                if to not in parents:
                    nxt.setdefault(to, []).append((frm, kind, c))
            if owner_step:
                # method -> module -> (object | form -> object) of type Документ
                for m, doc in conn.execute(
                        f"SELECT n.id, CASE WHEN o.kind='object' THEN o.id ELSE oo.id END FROM nodes n "
                        f"JOIN nodes md ON md.id = n.parent_id JOIN nodes o ON o.id = md.parent_id "
                        f"LEFT JOIN nodes oo ON oo.id = o.parent_id WHERE n.id IN ({q}) AND n.kind='method' AND "
                        f"((o.kind='object' AND o.type='Документ') OR (o.kind='form' AND oo.type='Документ'))", part):
                    if doc is not None and doc not in parents:
                        nxt.setdefault(doc, []).append((m, 'запись/проведение документа', 'heuristic'))
        for n, ps in nxt.items():
            parents[n] = ps
        level = list(nxt)
        found = [n for n in level if n in targets]
    paths = []

    def back(node, suffix):
        if len(paths) >= max_paths:
            return
        if not parents[node]:
            paths.append([(node, None, None)] + suffix)
            return
        for frm, kind, c in parents[node]:
            back(frm, [(node, kind, c)] + suffix)
            if len(paths) >= max_paths:
                return

    for t in found:
        back(t, [])
    return paths


def call_paths(conn, from_ids, to_ids, max_depth=6, kinds=('calls',), confidences=None, max_paths=5):
    """Shortest call paths: distances by a forward reach(), then backtracking over edges
    whose source is one level closer to the start."""
    dist = reach(conn, from_ids, 'out', kinds, max_depth, confidences, limit=200000)
    hits = [t for t in to_ids if t in dist]
    if not hits:
        return []
    best = min(dist[t] for t in hits)
    conf = list(confidences or ('exact', 'heuristic', 'unresolved'))
    paths = []

    def back(node, suffix):
        if len(paths) >= max_paths:
            return
        if dist.get(node) == 0:
            paths.append([node] + suffix)
            return
        for r in conn.execute(f'SELECT DISTINCT from_id FROM edges WHERE to_id=? AND kind IN '
                              f'({",".join("?" * len(kinds))}) AND confidence IN ({",".join("?" * len(conf))})',
                              [node, *kinds, *conf]):
            if dist.get(r[0]) == dist[node] - 1:
                back(r[0], [node] + suffix)
                if len(paths) >= max_paths:
                    return

    for t in hits:
        if dist[t] == best:
            back(t, [])
    return paths
