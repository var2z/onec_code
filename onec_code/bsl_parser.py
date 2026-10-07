"""BSL (1C:Enterprise language) module parser.

Splits a module into methods and extracts raw references (calls, register
writes, query tables, dynamic calls). Name resolution against metadata is done
later by the indexer; this module has no knowledge of the configuration.

The parser is token based (one C-level regex pass), so keywords inside strings
and comments are ignored and statements like ``КонецЕсли;КонецПроцедуры`` on a
single line are handled.
"""
from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------

# Multi-line string literal: continuation lines start with "|"; comment and
# blank lines are allowed between them.
_STR = r'"(?:[^"\n]|"")*(?:\n[ \t\r]*(?://[^\n]*|\|(?:[^"\n]|"")*|(?=\r?\n)))*"?'

_TOKEN_RE = re.compile(
    r'(?P<c>//[^\n]*)'
    r'|(?P<s>' + _STR + r')'
    r'|(?P<p>#[^\n]*)'
    r'|(?P<a>&[^\W\d]\w*)'
    r'|(?P<i>[^\W\d]\w*)'
    r'|(?P<n>\d+(?:\.\d+)?)'
    r"|(?P<d>'[^'\n]*')"
    r'|(?P<o><>|<=|>=|[().,;=+\-*/%<>\[\]?:~])'
)

# token kinds
ID, STR, PREP, ANNO, NUM, DATE, OP = 'i', 's', 'p', 'a', 'n', 'd', 'o'


class Tok:
    __slots__ = ('k', 'v', 'lo', 'pos')

    def __init__(self, k, v, pos):
        self.k = k
        self.v = v
        self.lo = v.lower() if k == ID else None
        self.pos = pos

    def __repr__(self):  # pragma: no cover - debug helper
        return f'Tok({self.k},{self.v!r})'


def tokenize(text: str) -> list[Tok]:
    out = []
    append = out.append
    for m in _TOKEN_RE.finditer(text):
        k = m.lastgroup
        if k == 'c':
            continue
        append(Tok(k, m.group(), m.start()))
    return out


# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

METHOD_KW = {'процедура': 'Процедура', 'функция': 'Функция',
             'procedure': 'Процедура', 'function': 'Функция'}
END_KW = {'конецпроцедуры', 'конецфункции', 'endprocedure', 'endfunction'}
EXPORT_KW = {'экспорт', 'export'}
ASYNC_KW = {'асинх', 'async'}
VAL_KW = {'знач', 'val'}

DIRECTIVES = {
    'наклиенте': 'НаКлиенте', 'atclient': 'НаКлиенте',
    'насервере': 'НаСервере', 'atserver': 'НаСервере',
    'насерверебезконтекста': 'НаСервереБезКонтекста', 'atservernocontext': 'НаСервереБезКонтекста',
    'наклиентенасерверебезконтекста': 'НаКлиентеНаСервереБезКонтекста',
    'atclientatservernocontext': 'НаКлиентеНаСервереБезКонтекста',
    'наклиентенасервере': 'НаКлиентеНаСервере', 'atclientatserver': 'НаКлиентеНаСервере',
}
EXT_ANNOTATIONS = {
    'перед': 'Перед', 'before': 'Перед',
    'после': 'После', 'after': 'После',
    'вместо': 'Вместо', 'around': 'Вместо',
    'изменениеиконтроль': 'ИзменениеИКонтроль', 'changeandvalidate': 'ИзменениеИКонтроль',
}

# Words that may be followed by "(" but are not calls.
NOT_CALL_KW = {
    'если', 'иначеесли', 'пока', 'для', 'каждого', 'из', 'возврат', 'новый', 'не', 'и', 'или',
    'тогда', 'цикл', 'по', 'вызватьисключение', 'ждать',
    'if', 'elsif', 'while', 'for', 'each', 'in', 'return', 'new', 'not', 'and', 'or',
    'then', 'do', 'to', 'raise', 'await',
}
# Tokens after which a new statement starts (used for assignment detection).
STMT_START_KW = {'тогда', 'иначе', 'цикл', 'попытка', 'исключение', 'экспорт',
                 'then', 'else', 'do', 'try', 'except', 'export'}
FOR_KW = {'для', 'for'}
EACH_KW = {'каждого', 'each'}

COMMON_MODULE_FN = {'общиймодуль', 'commonmodule'}
EXECUTE_FN = {'выполнить': 'Выполнить', 'execute': 'Выполнить',
              'вычислить': 'Вычислить', 'eval': 'Вычислить'}
NOTIFY_TYPES = {'описаниеоповещения', 'notifydescription'}
SELF_REFS = {'этотобъект', 'этаформа', 'thisobject', 'thisform'}
RECORDS_KW = {'движения', 'registerrecords'}

# Query language: source table types (singular).
QUERY_TYPES = {
    'справочник': 'Справочник', 'catalog': 'Справочник',
    'документ': 'Документ', 'document': 'Документ',
    'регистрнакопления': 'РегистрНакопления', 'accumulationregister': 'РегистрНакопления',
    'регистрсведений': 'РегистрСведений', 'informationregister': 'РегистрСведений',
    'регистрбухгалтерии': 'РегистрБухгалтерии', 'accountingregister': 'РегистрБухгалтерии',
    'регистррасчета': 'РегистрРасчета', 'calculationregister': 'РегистрРасчета',
    'планвидовхарактеристик': 'ПланВидовХарактеристик', 'chartofcharacteristictypes': 'ПланВидовХарактеристик',
    'плансчетов': 'ПланСчетов', 'chartofaccounts': 'ПланСчетов',
    'планвидоврасчета': 'ПланВидовРасчета', 'chartofcalculationtypes': 'ПланВидовРасчета',
    'планобмена': 'ПланОбмена', 'exchangeplan': 'ПланОбмена',
    'бизнеспроцесс': 'БизнесПроцесс', 'businessprocess': 'БизнесПроцесс',
    'задача': 'Задача', 'task': 'Задача',
    'константа': 'Константа', 'constant': 'Константа',
    'перечисление': 'Перечисление', 'enum': 'Перечисление',
    'журналдокументов': 'ЖурналДокументов', 'documentjournal': 'ЖурналДокументов',
    'последовательность': 'Последовательность', 'sequence': 'Последовательность',
}
_QT_ALT = '|'.join(sorted(QUERY_TYPES, key=len, reverse=True))
_QUERY_HINT_RE = re.compile(r'(?<![\w.])(?:из|from|соединение|join)\s', re.I)
# FROM clause start and the keywords that end it.
_FROM_RE = re.compile(r'(?<![\w.&])(?:из|from|соединение|join)(?![\w])', re.I)
_FROM_END_RE = re.compile(
    r'(?<![\w.&])(?:где|where|сгруппировать|group|упорядочить|order|объединить|union|имеющие|having|'
    r'итоги|totals|индексировать|index|поместить|into|выбрать|select|для\s+изменения|for\s+update|'
    r'автоупорядочивание|autoorder|уничтожить|drop)(?![\w])|;', re.I)
_SRC_RE = re.compile(
    r'(?<![\w.&])(' + _QT_ALT + r')\.([^\W\d]\w*)(?:\.([^\W\d]\w*))?', re.I)
# Things inside FROM that are not sources: ЗНАЧЕНИЕ(Справочник.X.Y), ССЫЛКА Документ.X
_NOT_SRC_BEFORE_RE = re.compile(r'(?:значение|value|ссылка|refs)\s*\(?\s*$', re.I)

_STR_METHOD_RE = re.compile(r'^([^\W\d]\w*)\.([^\W\d]\w*)$')

# Preprocessor
_PREP_RE = re.compile(r'#\s*([^\W\d]\w*)\s*(.*)', re.S)
PREP_REGION = {'область', 'region'}
PREP_ENDREGION = {'конецобласти', 'endregion'}
PREP_IF = {'если', 'if'}
PREP_ELSIF = {'иначеесли', 'elsif'}
PREP_ELSE = {'иначе', 'else'}
PREP_ENDIF = {'конецесли', 'endif'}
PREP_DELETE = {'удаление', 'delete'}
PREP_ENDDELETE = {'конецудаления', 'enddelete'}
PREP_INSERT = {'вставка', 'insert'}
PREP_ENDINSERT = {'конецвставки', 'endinsert'}


# --------------------------------------------------------------------------
# Result model
# --------------------------------------------------------------------------

@dataclass
class Param:
    name: str
    by_val: bool = False
    default: str | None = None


@dataclass
class Ref:
    """Raw reference found in code.

    kind:
      call     - target=(ident, ident, ...) chain ending with the called name
      dyn      - dynamic call; detail keys: type ('common_module' | 'execute' |
                 'notify' | 'string'), module, method, var, reason
      writes   - Движения.<target[0]>
      query    - target=(Тип, Имя, Подчинённое|None)
    """
    kind: str
    target: tuple
    line: int
    detail: dict | None = None


@dataclass
class Method:
    name: str
    kind: str                     # Процедура | Функция
    params: list[Param]
    export: bool
    is_async: bool
    directive: str | None
    annotations: list[tuple[str, str | None]]   # (name, string arg)
    regions: list[str]
    ifdef: list[str]
    line_start: int               # first annotation/directive line or header line
    line_header: int
    line_end: int
    doc_line: int                 # first line of the comment block above (== line_start if none)
    signature: str
    refs: list[Ref] = field(default_factory=list)
    broken: bool = False          # no matching КонецПроцедуры/КонецФункции
    directive_implicit: bool = False   # form module method without directive -> НаСервере

    @property
    def extension_kind(self) -> str | None:
        for a, _ in self.annotations:
            if a in ('Перед', 'После', 'Вместо', 'ИзменениеИКонтроль'):
                return a
        return None

    @property
    def extension_target(self) -> str | None:
        for a, arg in self.annotations:
            if a in ('Перед', 'После', 'Вместо', 'ИзменениеИКонтроль'):
                return arg
        return None


@dataclass
class Module:
    methods: list[Method]
    body_refs: list[Ref]          # references in module body (outside methods)
    line_count: int
    errors: list[str]


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

class _Lines:
    def __init__(self, text):
        self.starts = [0] + [m.end() for m in re.finditer('\n', text)]

    def __call__(self, pos):
        return bisect_right(self.starts, pos)


def _norm_prep(tok_v):
    m = _PREP_RE.match(tok_v)
    if not m:
        return '', ''
    return m.group(1).lower(), m.group(2).strip()


def _str_value(v: str) -> str:
    """Value of a string literal token (multi-line joined with \\n)."""
    body = v[1:-1] if len(v) >= 2 and v.endswith('"') else v[1:]
    if '\n' in body:
        parts = []
        for i, line in enumerate(body.split('\n')):
            line = line.rstrip('\r')
            if i == 0:
                parts.append(line)
                continue
            s = line.lstrip(' \t')
            if s.startswith('|'):
                parts.append(s[1:])
            # comment / blank lines inside the literal are skipped
        body = '\n'.join(parts)
    return body.replace('""', '"')


def parse_module(text: str, form_module: bool = False) -> Module:
    """Parse module text. form_module=True for managed form modules, where a method
    without a compilation directive is executed &НаСервере."""
    if text.startswith('﻿'):
        text = text[1:]
    toks = tokenize(text)
    line_of = _Lines(text)
    src_lines = text.split('\n')
    n = len(toks)
    errors: list[str] = []

    methods: list[Method] = []
    # (method, body_start_idx, body_end_idx) for ref extraction
    ranges: list[tuple[Method | None, int, int]] = []
    deleted: list[tuple[int, int]] = []           # token index ranges inside #Удаление

    regions: list[str] = []
    ifdefs: list[str] = []
    module_vars: set[str] = set()                   # module-level Перем
    pending: list[tuple[str, str | None, int]] = []  # annotations before a method
    cur: Method | None = None
    cur_body = 0
    outside_start = 0
    del_start = None
    i = 0
    while i < n:
        t = toks[i]
        k = t.k
        if k == PREP:
            kw, rest = _norm_prep(t.v)
            if kw in PREP_REGION:
                regions.append(rest.split()[0] if rest else '')
            elif kw in PREP_ENDREGION:
                if regions:
                    regions.pop()
                else:
                    errors.append(f'line {line_of(t.pos)}: #КонецОбласти without #Область')
            elif kw in PREP_IF:
                ifdefs.append(_strip_then(rest))
            elif kw in PREP_ELSIF:
                if ifdefs:
                    ifdefs[-1] = _strip_then(rest)
            elif kw in PREP_ELSE:
                if ifdefs:
                    ifdefs[-1] = 'Не (' + ifdefs[-1] + ')'
            elif kw in PREP_ENDIF:
                if ifdefs:
                    ifdefs.pop()
            elif kw in PREP_DELETE:
                del_start = i
            elif kw in PREP_ENDDELETE:
                if del_start is not None:
                    deleted.append((del_start, i))
                    del_start = None
            i += 1
            continue

        if k == ANNO:
            name = t.v[1:]
            lo = name.lower()
            name = DIRECTIVES.get(lo) or EXT_ANNOTATIONS.get(lo) or name
            arg = None
            j = i + 1
            if j < n and toks[j].k == OP and toks[j].v == '(':
                depth = 0
                while j < n:
                    tj = toks[j]
                    if tj.k == OP and tj.v == '(':
                        depth += 1
                    elif tj.k == OP and tj.v == ')':
                        depth -= 1
                        if depth == 0:
                            break
                    elif tj.k == STR and arg is None:
                        arg = _str_value(tj.v)
                    j += 1
                i = j
            if cur is None:
                pending.append((name, arg, t.pos))
            i += 1
            continue

        if k == ID and (i == 0 or not (toks[i - 1].k == OP and toks[i - 1].v == '.')):
            lo = t.lo
            is_async = False
            kw_idx = i
            if lo in ASYNC_KW and i + 1 < n and toks[i + 1].k == ID and toks[i + 1].lo in METHOD_KW:
                is_async = True
                kw_idx = i + 1
                lo = toks[kw_idx].lo
            if lo in METHOD_KW:
                if cur is not None:
                    # Unterminated previous method: close it right before this one.
                    cur.line_end = line_of(t.pos) - 1
                    cur.broken = True
                    errors.append(f'line {cur.line_header}: method {cur.name} has no end')
                    ranges.append((cur, cur_body, i))
                    cur = None
                else:
                    ranges.append((None, outside_start, i))
                m, body_idx = _parse_header(toks, kw_idx, is_async, pending, regions, ifdefs,
                                            text, line_of, src_lines, t.pos)
                pending = []
                if m is None:
                    errors.append(f'line {line_of(t.pos)}: bad method header')
                    outside_start = i + 1
                    i += 1
                    continue
                cur = m
                cur_body = body_idx
                i = body_idx
                continue
            if cur is None and lo in ('перем', 'var'):
                pending = []      # &НаКлиенте Перем X;  - directive belongs to the variable
                j = i + 1
                while j < n and not (toks[j].k == OP and toks[j].v == ';'):
                    if toks[j].k == ID and toks[j].lo not in EXPORT_KW:
                        module_vars.add(toks[j].lo)
                    j += 1
                i = j
                continue
            if lo in END_KW:
                if cur is not None:
                    cur.line_end = line_of(t.pos)
                    methods.append(cur)
                    ranges.append((cur, cur_body, i))
                    cur = None
                    outside_start = i + 1
                else:
                    errors.append(f'line {line_of(t.pos)}: {t.v} without method')
                i += 1
                continue
        i += 1

    if cur is not None:
        cur.line_end = len(src_lines)
        cur.broken = True
        errors.append(f'line {cur.line_header}: method {cur.name} has no end')
        methods.append(cur)
        ranges.append((cur, cur_body, n))
    else:
        ranges.append((None, outside_start, n))
    # methods closed as broken on the next header were not appended yet
    seen = {id(m) for m in methods}
    for m, _, _ in ranges:
        if m is not None and id(m) not in seen:
            methods.append(m)
            seen.add(id(m))
    methods.sort(key=lambda m: m.line_header)
    if form_module:
        for m in methods:
            if m.directive is None:
                m.directive = 'НаСервере'
                m.directive_implicit = True

    del_mask = None
    if deleted:
        del_mask = bytearray(n)
        for a, b in deleted:
            for x in range(a, b + 1):
                del_mask[x] = 1

    body_refs: list[Ref] = []
    for m, a, b in ranges:
        known_locals = module_vars | {p.name.lower() for p in m.params} if m is not None else module_vars
        refs = _extract_refs(toks, a, b, line_of, del_mask, known_locals)
        if m is None:
            body_refs.extend(refs)
        else:
            m.refs = refs
    return Module(methods=methods, body_refs=body_refs, line_count=len(src_lines), errors=errors)


def _strip_then(s: str) -> str:
    s = s.strip()
    low = s.lower()
    for w in (' тогда', ' then'):
        if low.endswith(w):
            return s[: -len(w)].strip()
    return s


def _parse_header(toks, kw_idx, is_async, pending, regions, ifdefs, text, line_of, src_lines, start_pos):
    n = len(toks)
    kind = METHOD_KW[toks[kw_idx].lo]
    j = kw_idx + 1
    if j >= n or toks[j].k != ID:
        return None, kw_idx + 1
    name = toks[j].v
    j += 1
    params: list[Param] = []
    if j < n and toks[j].k == OP and toks[j].v == '(':
        j += 1
        depth = 1
        cur_p: Param | None = None
        default_from = None
        while j < n:
            tj = toks[j]
            if tj.k == OP and tj.v == '(':
                depth += 1
            elif tj.k == OP and tj.v == ')':
                depth -= 1
                if depth == 0:
                    break
            elif depth == 1 and tj.k == OP and tj.v == ',':
                if cur_p is not None:
                    if default_from is not None:
                        cur_p.default = _join(toks, default_from, j)
                    params.append(cur_p)
                cur_p, default_from = None, None
            elif depth == 1 and tj.k == OP and tj.v == '=' and cur_p is not None:
                default_from = j + 1
            elif depth == 1 and tj.k == ID and cur_p is None:
                if tj.lo in VAL_KW:
                    cur_p = Param(name='', by_val=True)
                else:
                    cur_p = Param(name=tj.v)
            elif depth == 1 and tj.k == ID and cur_p is not None and not cur_p.name:
                cur_p.name = tj.v
            j += 1
        if cur_p is not None:
            if default_from is not None:
                cur_p.default = _join(toks, default_from, j)
            params.append(cur_p)
        close_idx = j
        j += 1
    else:
        close_idx = j - 1
    export = False
    if j < n and toks[j].k == ID and toks[j].lo in EXPORT_KW:
        export = True
        close_idx = j
        j += 1

    header_pos = toks[kw_idx - 1].pos if is_async else toks[kw_idx].pos
    line_header = line_of(header_pos)
    line_start = line_of(pending[0][2]) if pending else line_header
    directive = None
    annotations = []
    for a, arg, _ in pending:
        if a in DIRECTIVES.values() and directive is None:
            directive = a
        else:
            annotations.append((a, arg))

    end_tok = toks[min(close_idx, n - 1)]
    sig = text[header_pos:end_tok.pos + len(end_tok.v)]
    sig = re.sub(r'//[^\n]*', '', sig)
    sig = ' '.join(sig.split())
    sig = sig.replace('( ', '(').replace(' )', ')')

    doc = line_start
    while doc - 2 >= 0 and src_lines[doc - 2].strip().startswith('//'):
        doc -= 1

    m = Method(name=name, kind=kind, params=params, export=export, is_async=is_async,
               directive=directive, annotations=annotations, regions=list(regions),
               ifdef=list(ifdefs), line_start=line_start, line_header=line_header,
               line_end=line_header, doc_line=doc, signature=sig)
    return m, j


def _join(toks, a, b):
    return ''.join(t.v for t in toks[a:b]).strip() or None


def _is_stmt_start(toks, idx, a):
    """True if token idx starts a statement (idx is within body starting at a)."""
    if idx <= a:
        return True
    p = toks[idx - 1]
    if p.k == OP:
        return p.v == ';'
    if p.k in (PREP, ANNO):
        return True
    if p.k == ID:
        return p.lo in STMT_START_KW
    return False


def _extract_refs(toks, a, b, line_of, del_mask, known_locals=frozenset()) -> list[Ref]:
    refs: list[Ref] = []
    if a >= b:
        return refs

    # Pass 1: assignments of variables. Value: module name (literal ОбщийМодуль("X")),
    # _NONLIT (ОбщийМодуль(expr)) or None (anything else).
    assigns: dict[str, list] = {}       # var -> [(token idx, value)]
    local_vars = set(known_locals)      # parameters, Перем, assigned names: shadow common modules
    for i in range(a, b):
        t = toks[i]
        if t.k != ID or del_mask is not None and del_mask[i]:
            continue
        prev = toks[i - 1] if i > a else None
        nx = toks[i + 1] if i + 1 < b else None
        if t.lo in ('перем', 'var') and (prev is None or _is_stmt_start(toks, i, a)):
            k = i + 1
            while k < b and not (toks[k].k == OP and toks[k].v == ';'):
                if toks[k].k == ID:
                    local_vars.add(toks[k].lo)
                k += 1
            continue
        if prev is not None and prev.k == ID and prev.lo in EACH_KW:
            assigns.setdefault(t.lo, []).append((i, None))      # Для Каждого М Из ...
            continue
        if nx is None or nx.k != OP or nx.v != '=':
            continue
        is_for = prev is not None and prev.k == ID and prev.lo in FOR_KW
        if not (is_for or _is_stmt_start(toks, i, a)):
            continue
        cm = None if is_for else _match_common_module(toks, i + 2, b)
        if cm and cm[1] < b and toks[cm[1]].v == '.':
            cm = None     # М = ...ОбщийМодуль("X").Метод()  - result of the call, not the module
        assigns.setdefault(t.lo, []).append((i, cm[0] if cm else None))
    local_vars.update(assigns)
    module_vars = {v for v, lst in assigns.items() if any(val is not None for _, val in lst)}
    ctx_at = _block_contexts(toks, a, b) if module_vars else None

    # Pass 2
    i = a
    while i < b:
        if del_mask is not None and del_mask[i]:
            i += 1
            continue
        t = toks[i]
        k = t.k
        if k == STR:
            _string_refs(t, refs, line_of)
            i += 1
            continue
        if k != ID:
            i += 1
            continue
        prev = toks[i - 1] if i > 0 else None
        after_dot = prev is not None and prev.k == OP and prev.v == '.'
        lo = t.lo

        # Движения.X  (also ЭтотОбъект.Движения.X)
        if lo in RECORDS_KW and i + 2 < b and toks[i + 1].v == '.' and toks[i + 2].k == ID:
            if not (i + 3 < b and toks[i + 3].v == '('):
                refs.append(Ref('writes', (toks[i + 2].v,), line_of(t.pos)))

        if after_dot:
            i += 1
            continue

        if lo in ('новый', 'new'):
            if i + 2 < b and toks[i + 1].k == ID and toks[i + 1].lo in NOTIFY_TYPES and toks[i + 2].v == '(':
                args = _call_args(toks, i + 2, b)
                meth = None
                target = None
                if args:
                    a0 = args[0]
                    if len(a0) == 1 and a0[0].k == STR:
                        meth = _str_value(a0[0].v)
                    if len(args) > 1:
                        target = ''.join(x.v for x in args[1]) or None
                refs.append(Ref('dyn', (), line_of(t.pos),
                                {'type': 'notify', 'method': meth, 'target': target}))
            i += 2   # skip the type name: "Новый Структура(" is not a call
            continue

        # identifier chain  A.B.C(
        j = i
        chain = [t.v]
        while j + 2 < b and toks[j + 1].k == OP and toks[j + 1].v == '.' and toks[j + 2].k == ID:
            j += 2
            chain.append(toks[j].v)
        is_call = j + 1 < b and toks[j + 1].k == OP and toks[j + 1].v == '('
        line = line_of(t.pos)

        if is_call and len(chain) <= 2 and chain[-1].lower() in COMMON_MODULE_FN:
            refs.append(Ref('call', tuple(chain), line))   # the real call to ...ОбщийМодуль()
            cm = _match_common_module(toks, i, b)
            mod = cm[0] if cm else _NONLIT
            after = cm[1] if cm else j + 1
            lit = None if mod is _NONLIT else mod
            if after + 2 < b and toks[after].v == '.' and toks[after + 1].k == ID and toks[after + 2].v == '(':
                d = {'type': 'common_module', 'module': lit, 'method': toks[after + 1].v}
                if lit is None:
                    d['reason'] = 'non-literal'
                refs.append(Ref('dyn', tuple(chain), line, d))
            elif not (i >= 2 and toks[i - 1].v == '=' and toks[i - 2].k == ID):
                # neither called nor bound to a variable (passed as an argument etc.)
                refs.append(Ref('dyn', tuple(chain), line,
                                {'type': 'common_module', 'module': lit,
                                 'reason': 'non-literal' if lit is None else 'no-call'}))
            i += 1
            continue

        if len(chain) == 1 and is_call and lo in EXECUTE_FN:
            refs.append(Ref('dyn', (t.v,), line, {'type': 'execute', 'fn': EXECUTE_FN[lo]}))
            i += 1
            continue

        if len(chain) == 2 and is_call and lo in module_vars:
            res = _resolve_var(assigns[lo], i, ctx_at, a)
            if res is not None:
                mod, reason = res
                d = {'type': 'common_module', 'module': mod, 'method': chain[1], 'var': t.v}
                if reason:
                    d['reason'] = reason
                refs.append(Ref('dyn', tuple(chain), line, d))
                i += 1
                continue

        if is_call and not (len(chain) == 1 and lo in NOT_CALL_KW):
            d = _literal_args(toks, j + 1, b)
            if len(chain) > 1 and lo in local_vars:
                d = {**(d or {}), 'l': 1}      # qualifier is a local name, not a common module
            refs.append(Ref('call', tuple(chain), line, d))
        i += 1      # inner chain members are visited as after-dot tokens (Движения check)
    return refs


BLOCK_OPEN = {'если', 'if', 'пока', 'while', 'для', 'for', 'попытка', 'try'}
BLOCK_BRANCH = {'иначеесли', 'elsif', 'иначе', 'else', 'исключение', 'except'}
BLOCK_CLOSE = {'конецесли', 'endif', 'конеццикла', 'enddo', 'конецпопытки', 'endtry'}


def _block_contexts(toks, a, b):
    """Block context of every token in [a, b): tuple of branch ids.

    Each If/ElsIf/Else branch, loop body and Try/Except part gets its own id, so an
    assignment dominates a position (in straight-line order) iff its context is a
    prefix of the position's context.
    """
    stack: list[int] = []
    nid = 0
    ctx = ()
    out = [()] * (b - a)
    for i in range(a, b):
        t = toks[i]
        if t.k == ID and not (i > 0 and toks[i - 1].k == OP and toks[i - 1].v == '.'):
            lo = t.lo
            if lo in BLOCK_OPEN:
                nid += 1
                stack.append(nid)
                ctx = tuple(stack)
            elif lo in BLOCK_BRANCH and stack:
                nid += 1
                stack[-1] = nid
                ctx = tuple(stack)
            elif lo in BLOCK_CLOSE and stack:
                stack.pop()
                ctx = tuple(stack)
        out[i - a] = ctx
    return out


def _resolve_var(assign_list, pos, ctx_at, a):
    """Resolve a module variable at token pos by the nearest preceding assignments.

    Walks assignments backwards until one that dominates pos (same or enclosing
    block); collects values of all non-dominating ones passed on the way (they are
    in branches that may or may not have executed).
    Returns (module | None, reason | None), or None if the variable does not hold a
    module here (plain call).
    """
    pctx = ctx_at[pos - a]
    vals = []
    for idx, val in reversed(assign_list):
        if idx >= pos:
            continue
        vals.append(val)
        actx = ctx_at[idx - a]
        if pctx[:len(actx)] == actx:
            break           # dominating assignment: earlier ones are overwritten
    if not vals:
        return None, 'no-preceding-assignment'
    if all(v is None for v in vals):
        return None     # variable holds something else here
    if any(v is _NONLIT for v in vals):
        return None, 'non-literal'
    # Non-module values (e.g. М = Неопределено before an optional branch) cannot
    # receive a method call, so only the literal module candidates matter.
    lits = {v.lower(): v for v in vals if v is not None}
    if len(lits) == 1:
        return next(iter(lits.values())), None
    return None, 'ambiguous-branches'


class _NonLit:
    def __repr__(self):  # pragma: no cover
        return '<non-literal>'


_NONLIT = _NonLit()


def _match_common_module(toks, i, b):
    """Match  [Q.]ОбщийМодуль("X")  starting at i. Returns (module|_NONLIT, idx after ')') or None."""
    j = i
    if j < b and toks[j].k == ID and toks[j].lo not in COMMON_MODULE_FN:
        if j + 2 < b and toks[j + 1].v == '.' and toks[j + 2].k == ID:
            j += 2
        else:
            return None
    if not (j < b and toks[j].k == ID and toks[j].lo in COMMON_MODULE_FN):
        return None
    if not (j + 1 < b and toks[j + 1].v == '('):
        return None
    if j + 3 < b and toks[j + 2].k == STR and toks[j + 3].v == ')':
        return _str_value(toks[j + 2].v).strip(), j + 4
    # non-literal: skip to closing paren
    depth = 0
    k = j + 1
    while k < b:
        if toks[k].v == '(':
            depth += 1
        elif toks[k].v == ')':
            depth -= 1
            if depth == 0:
                return _NONLIT, k + 1
        k += 1
    return _NONLIT, b


_LITERAL_ARG_RE = re.compile(r'^[\w.]{1,120}$')


def _literal_args(toks, open_idx, b):
    """Identifier-like string literal arguments of a call: {'s': [[arg index, value], ...]} or None.

    Used for data-driven dispatch, e.g. ПроведениеДокументов.ОтразитьДвижения(Т, Д, "ИмяРегистра")
    or МеханизмыДокумента.Добавить("Продажи")."""
    out = None
    depth = 0
    idx = 0
    ntok = 0
    lit = None
    k = open_idx
    while k < b:
        t = toks[k]
        if t.k == OP and t.v in '([':
            depth += 1
            if depth == 1:
                k += 1
                continue
            ntok += 1
        elif t.k == OP and t.v in ')]':
            depth -= 1
            if depth == 0:
                break
            ntok += 1
        elif depth == 1 and t.k == OP and t.v == ',':
            if ntok == 1 and lit is not None:
                (out := out or []).append([idx, lit])
            idx += 1
            ntok = 0
            lit = None
            k += 1
            continue
        else:
            ntok += 1
            if depth == 1 and ntok == 1 and t.k == STR and '\n' not in t.v:
                v = _str_value(t.v)
                if _LITERAL_ARG_RE.match(v):
                    lit = v
        k += 1
    if ntok == 1 and lit is not None:
        (out := out or []).append([idx, lit])
    return {'s': out} if out else None


def _call_args(toks, open_idx, b):
    """Split call arguments (token lists) for a call whose '(' is at open_idx."""
    args = [[]]
    depth = 0
    k = open_idx
    while k < b:
        t = toks[k]
        if t.k == OP and t.v in '([':
            depth += 1
            if depth == 1:
                k += 1
                continue
        elif t.k == OP and t.v in ')]':
            depth -= 1
            if depth == 0:
                break
        elif depth == 1 and t.k == OP and t.v == ',':
            args.append([])
            k += 1
            continue
        args[-1].append(t)
        k += 1
    if args == [[]]:
        return []
    return args


def _string_refs(t: Tok, refs: list[Ref], line_of):
    v = t.v
    # "Модуль.Метод" as a whole literal
    if len(v) < 200 and '.' in v:
        s = _str_value(v).strip()
        m = _STR_METHOD_RE.match(s)
        if m:
            refs.append(Ref('dyn', (m.group(1), m.group(2)), line_of(t.pos),
                            {'type': 'string', 'module': m.group(1), 'method': m.group(2)}))
            return
    if not _QUERY_HINT_RE.search(v):
        return
    for q in extract_query_sources(_str_value(v)):
        refs.append(Ref('query', q[:3], line_of(t.pos) + q[3]))


def extract_query_sources(q: str) -> list[tuple[str, str, str | None, int]]:
    """Find data sources in FROM/JOIN clauses of a query text.

    Returns (Тип, Имя, Подчинённое|None, line_offset) with canonical Russian type names.
    """
    out = []
    seen = set()
    pos = 0
    while True:
        m = _FROM_RE.search(q, pos)
        if not m:
            break
        start = m.end()
        e = _FROM_END_RE.search(q, start)
        end = e.start() if e else len(q)
        # a nested FROM (subquery / virtual table params) is scanned on its own
        nxt = _FROM_RE.search(q, start)
        if nxt and nxt.start() < end:
            end = nxt.start()
        seg = q[start:end]
        for s in _SRC_RE.finditer(seg):
            if _NOT_SRC_BEFORE_RE.search(seg[max(0, s.start() - 40):s.start()]):
                continue
            typ = QUERY_TYPES[s.group(1).lower()]
            key = (typ, s.group(2).lower(), (s.group(3) or '').lower())
            if key in seen:
                continue
            seen.add(key)
            out.append((typ, s.group(2), s.group(3), q.count('\n', 0, start + s.start())))
        pos = max(end, m.end())
    return out
