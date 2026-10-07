"""Configuration profiles: heuristics specific to a family of typical configurations.

The universal part of the graph (graph.py) follows platform rules only: modules, metadata,
subscriptions, forms, calls, RegisterRecords, Движения.X. A profile adds edges that rely on
conventions of particular configurations and is enabled by the configuration name from
Configuration.xml. An unknown configuration gets no profile, so no convention-based edges.

Profiles
  up   1С:Управление предприятием family (УТ 11, КА 2, ERP 2): generic posting engine
       ПроведениеДокументов — accounting mechanisms registered by documents, mechanism modules'
       ОтразитьДвижения, literal register names in ОтразитьДвижения / ТребуетсяТаблицаДляДвижений.
  unf  1С:Управление нашей фирмой 3: ПроведениеДокументовУНФ.ОтразитьДвижения("Регистр", …) called from
       the document's ОбработкаПроведения.
"""
from __future__ import annotations

from .layout import MANAGER_COLLECTIONS


class Profile:
    code = ''
    title = ''
    config_names: frozenset = frozenset()
    description = ''

    def matches(self, config_name: str) -> bool:
        return (config_name or '').casefold() in self.config_names

    def keep_module(self, full_lower: str) -> bool:
        """Modules whose parse results extra_edges() needs."""
        return False

    def on_call(self, ctx, sink, frm, to, target, line, detail):
        """Called for every resolved call edge frm -> to."""

    def extra_edges(self, ctx, sink, parsed: dict):
        """Called once after universal edges; parsed = {module id: parse result} of kept modules."""


def _posting_helper_role(key: str) -> str | None:
    if 'движени' not in key:
        return None
    if any(w in key for w in ('отразитьдвижени', 'записатьдвижени', 'сформироватьдвижени', 'записьдвижени')):
        return 'запись'
    if any(w in key for w in ('таблицадлядвижени', 'таблицыдлядвижени', 'текстзапроса')):
        return 'подготовка таблицы движений'
    return None


def _own_records(ctx, frm) -> set:
    """Register ids from RegisterRecords of the document whose module contains method frm."""
    owner = ctx.module_owner.get(ctx.meth_module.get(frm))
    while owner is not None and ctx.node_kind.get(owner) != 'object':      # form module -> form -> object
        owner = ctx.form_parent.get(owner)
    props = ctx.blob.get(owner, {}).get('props', {}) if owner else {}
    return {r for r in (ctx.obj_by_full(x) for x in props.get('RegisterRecords', [])) if r}


def _literal_register_writes(profile, ctx, sink, frm, to, target, line, detail, exact_own=False):
    """Literal register names passed to posting helpers: ОтразитьДвижения(…, "Рег") -> writes,
    ТребуетсяТаблицаДляДвижений("Рег") -> prepares. exact_own: exact when the register is in
    RegisterRecords of the calling method's own document."""
    if not detail or not detail.get('s'):
        return
    role = _posting_helper_role(ctx.meth_key.get(to, ''))
    if not role:
        return
    own = _own_records(ctx, frm) if exact_own else set()
    for _, v in detail['s']:
        for reg in ctx.registers.get(v.lower(), ()):
            sink.add(frm, reg, 'writes_register', 'exact' if reg in own else 'heuristic', line,
                     {'via': target[-1], 'profile': profile.code, **({'role': role} if role != 'запись' else {})})


def _method_refs(ctx, parsed, method_node):
    p = parsed.get(ctx.meth_module.get(method_node))
    o = ctx.meth_pos.get(method_node)
    return p['r'][o] if p and o is not None and o < len(p['r']) else []


class UPProfile(Profile):
    code = 'up'
    title = 'УП (УТ/КА/ERP): механизмы проведения ПроведениеДокументов'
    config_names = frozenset(x.casefold() for x in (
        'УправлениеТорговлей', 'КомплекснаяАвтоматизация', 'УправлениеПредприятием'))
    description = ('документ → механизмы (ЗарегистрироватьУчетныеМеханизмы / УчетныеМеханизмыКонфигурации) → '
                   '<модуль>.ОтразитьДвижения → ПроведениеДокументов.ОтразитьДвижения(…, "Регистр")')

    def keep_module(self, full_lower):
        return full_lower.startswith('общиймодуль.проведениедокументов.') or (
            full_lower.startswith('документ.') and full_lower.endswith('.модульменеджера'))

    def on_call(self, ctx, sink, frm, to, target, line, detail):
        _literal_register_writes(self, ctx, sink, frm, to, target, line, detail)

    def extra_edges(self, ctx, sink, parsed):
        """документ -> <модуль механизма>.ОтразитьДвижения и -> <менеджер>.ДанныеДокументаДляПроведения."""
        eng = ctx.common.get('проведениедокументов')
        cfg = ctx.meth[eng].get('учетныемеханизмыконфигурации') if eng else None
        if not cfg:
            return
        mech_map = {}
        for r in _method_refs(ctx, parsed, cfg[0]):
            if r[0] == 'call' and r[1][-1].lower() == 'вставить' and r[3]:
                s = dict((i, v) for i, v in r[3].get('s', []))
                if 0 in s and 1 in s:
                    mech_map[s[0].lower()] = s[1]
        targets = {}
        for mech, mod in mech_map.items():
            if '.' in mod:
                coll, _, name = mod.partition('.')
                t = MANAGER_COLLECTIONS.get(coll.lower())
                mid = ctx.by_full.get(f'{t}.{name}.модульменеджера'.lower()) if t else None
            else:
                mid = ctx.common.get(mod.lower())
            m = ctx.meth.get(mid, {}).get('отразитьдвижения') if mid else None
            targets[mech] = (m[0] if m else None, mod)
        for doc in ctx.obj_by_type.get('Документ', []):
            name = ctx.blob.get(doc, {}).get('name', '')
            mm = ctx.by_full.get(f'документ.{name}.модульменеджера'.lower())
            reg = ctx.meth[mm].get('зарегистрироватьучетныемеханизмы') if mm else None
            if not reg:
                continue
            dd = ctx.meth[mm].get('данныедокументадляпроведения')
            if dd:
                sink.add(doc, dd[0], 'posting_mechanism', 'heuristic', None,
                         {'step': 'ДанныеДокументаДляПроведения', 'profile': self.code})
            for r in _method_refs(ctx, parsed, reg[0]):
                if r[0] == 'call' and r[1][-1].lower() == 'добавить' and r[3]:
                    for i, v in r[3].get('s', []):
                        if i != 0:
                            continue
                        to, mod = targets.get(v.lower(), (None, None))
                        if to:
                            sink.add(doc, to, 'posting_mechanism', 'heuristic', r[2],
                                     {'mechanism': v, 'module': mod, 'profile': self.code})
                        elif v.lower() in mech_map:
                            sink.add(doc, None, 'posting_mechanism', 'broken', r[2],
                                     {'mechanism': v, 'target': f'{mod}.ОтразитьДвижения', 'profile': self.code},
                                     tkey=v.lower())


class UNFProfile(Profile):
    code = 'unf'
    title = 'УНФ: ПроведениеДокументовУНФ.ОтразитьДвижения("Регистр", …)'
    config_names = frozenset(x.casefold() for x in ('УправлениеНебольшойФирмой',))
    description = ('ОбработкаПроведения → ПроведениеДокументовУНФ.ОтразитьДвижения("Регистр", ТаблицыДляДвижений, '
                   'Движения, Отказ): регистр по литералу; exact, если он в RegisterRecords документа')

    def on_call(self, ctx, sink, frm, to, target, line, detail):
        _literal_register_writes(self, ctx, sink, frm, to, target, line, detail, exact_own=True)


PROFILES: dict[str, Profile] = {p.code: p for p in (UPProfile(), UNFProfile())}
UNIVERSAL = 'универсальный (только правила платформы)'


def select(config_name: str, override: str | None = None) -> Profile | None:
    """override: None/'auto' — by configuration name; 'none' — universal only; a profile code — force it."""
    ov = (override or 'auto').strip().lower()
    if ov == 'none':
        return None
    if ov != 'auto':
        if ov not in PROFILES:
            raise ValueError(f'Неизвестный профиль «{override}». Есть: auto, none, {", ".join(PROFILES)}')
        return PROFILES[ov]
    return next((p for p in PROFILES.values() if p.matches(config_name)), None)


def describe(code: str | None) -> str:
    if not code:
        return f'Профиль: {UNIVERSAL} — движения: RegisterRecords и Движения.X в коде'
    p = PROFILES.get(code)
    return f'Профиль: {p.title}' if p else f'Профиль: {code}'
