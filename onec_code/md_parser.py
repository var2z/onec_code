"""Parser of configuration metadata XML (format of DumpConfigToFiles, version 2.x).

Produces plain dataclasses; nothing here touches the database. Type references
are kept in a compact string form (see parse_type) and expanded against defined
types / characteristic types with expand_types().
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .layout import EN_TO_RU

NS_MD = 'http://v8.1c.ru/8.3/MDClasses'
NS_V8 = 'http://v8.1c.ru/8.1/data/core'
NS_XR = 'http://v8.1c.ru/8.3/xcf/readable'
NS_LF = 'http://v8.1c.ru/8.3/xcf/logform'
NS_XSI = 'http://www.w3.org/2001/XMLSchema-instance'

_MD = '{%s}' % NS_MD
_V8 = '{%s}' % NS_V8
_XR = '{%s}' % NS_XR
_LF = '{%s}' % NS_LF


def _local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]


# ----------------------------------------------------------------- events

SUBSCRIPTION_EVENTS = {
    'BeforeWrite': 'ПередЗаписью', 'OnWrite': 'ПриЗаписи', 'BeforeDelete': 'ПередУдалением',
    'Posting': 'ОбработкаПроведения', 'UndoPosting': 'ОбработкаУдаленияПроведения',
    'Filling': 'ОбработкаЗаполнения', 'FillCheckProcessing': 'ОбработкаПроверкиЗаполнения',
    'OnCopy': 'ПриКопировании', 'OnSetNewNumber': 'ПриУстановкеНовогоНомера',
    'OnSetNewCode': 'ПриУстановкеНовогоКода', 'FormGetProcessing': 'ОбработкаПолученияФормы',
    'PresentationGetProcessing': 'ОбработкаПолученияПредставления',
    'PresentationFieldsGetProcessing': 'ОбработкаПолученияПолейПредставления',
    'ChoiceDataGetProcessing': 'ОбработкаПолученияДанныхВыбора',
    'OnSendDataToSlave': 'ПриОтправкеДанныхПодчиненному', 'OnSendDataToMaster': 'ПриОтправкеДанныхГлавному',
    'OnReceiveDataFromSlave': 'ПриПолученииДанныхОтПодчиненного',
    'OnReceiveDataFromMaster': 'ПриПолученииДанныхОтГлавного',
    'OnSendNodeDataToSlave': 'ПриОтправкеДанныхУзлаПодчиненному',
    'ProcessingCheckUnique': 'ОбработкаПроверкиУникальности',
}

FORM_EVENTS = {
    'OnCreateAtServer': 'ПриСозданииНаСервере', 'OnOpen': 'ПриОткрытии', 'BeforeClose': 'ПередЗакрытием',
    'OnClose': 'ПриЗакрытии', 'OnReopen': 'ПриПовторномОткрытии',
    'BeforeWrite': 'ПередЗаписью', 'BeforeWriteAtServer': 'ПередЗаписьюНаСервере',
    'OnWriteAtServer': 'ПриЗаписиНаСервере', 'AfterWrite': 'ПослеЗаписи',
    'AfterWriteAtServer': 'ПослеЗаписиНаСервере', 'OnReadAtServer': 'ПриЧтенииНаСервере',
    'NotificationProcessing': 'ОбработкаОповещения', 'ChoiceProcessing': 'ОбработкаВыбора',
    'FillCheckProcessingAtServer': 'ОбработкаПроверкиЗаполненияНаСервере',
    'OnLoadDataFromSettingsAtServer': 'ПриЗагрузкеДанныхИзНастроекНаСервере',
    'BeforeLoadDataFromSettingsAtServer': 'ПередЗагрузкойДанныхИзНастроекНаСервере',
    'OnSaveDataInSettingsAtServer': 'ПриСохраненииДанныхВНастройкахНаСервере',
    'ExternalEvent': 'ВнешнееСобытие', 'NewWriteProcessing': 'ОбработкаЗаписиНового',
    'ActivationProcessing': 'ОбработкаАктивизации', 'URLProcessing': 'ОбработкаНавигационнойСсылки',
    'OnChange': 'ПриИзменении', 'StartChoice': 'НачалоВыбора', 'Clearing': 'Очистка',
    'AutoComplete': 'АвтоПодбор', 'TextEditEnd': 'ОкончаниеВводаТекста', 'Opening': 'Открытие',
    'Tuning': 'Регулирование', 'EditTextChange': 'ИзменениеТекстаРедактирования',
    'StartListChoice': 'НачалоВыбораИзСписка', 'Selection': 'Выбор', 'ValueChoice': 'ВыборЗначения',
    'OnActivateRow': 'ПриАктивизацииСтроки', 'OnActivateField': 'ПриАктивизацииПоля',
    'OnActivateCell': 'ПриАктивизацииЯчейки', 'BeforeAddRow': 'ПередНачаломДобавления',
    'BeforeDeleteRow': 'ПередУдалением', 'AfterDeleteRow': 'ПослеУдаления',
    'BeforeRowChange': 'ПередНачаломИзменения', 'BeforeEditEnd': 'ПередОкончаниемРедактирования',
    'OnStartEdit': 'ПриНачалеРедактирования', 'OnEditEnd': 'ПриОкончанииРедактирования',
    'OnGetDataAtServer': 'ПриПолученииДанныхНаСервере', 'DragStart': 'НачалоПеретаскивания',
    'DragCheck': 'ПроверкаПеретаскивания', 'Drag': 'Перетаскивание', 'DragEnd': 'ОкончаниеПеретаскивания',
    'OnCurrentPageChange': 'ПриСменеСтраницы', 'Click': 'Нажатие', 'OnActivate': 'ПриАктивизации',
    'BeforeExpand': 'ПередРазворачиванием', 'BeforeCollapse': 'ПередСворачиванием',
    'OnExpand': 'ПриРазворачивании', 'OnCollapse': 'ПриСворачивании',
    'DetailProcessing': 'ОбработкаРасшифровки', 'AdditionalDetailProcessing': 'ОбработкаДополнительнойРасшифровки',
    'ChoiceDataGetProcessing': 'ОбработкаПолученияДанныхВыбора',
    'OnActivateDate': 'ПриАктивизацииДаты', 'OnPeriodOutput': 'ПриВыводеПериода',
    'DocumentComplete': 'ДокументСформирован', 'OnClick': 'ПриНажатии',
    'BeforeLoadUserSettingsAtServer': 'ПередЗагрузкойПользовательскихНастроекНаСервере',
    'OnLoadUserSettingsAtServer': 'ПриЗагрузкеПользовательскихНастроекНаСервере',
    'OnUpdateUserSettingSetAtServer': 'ПриОбновленииСоставаПользовательскихНастроекНаСервере',
    'OnSaveUserSettingsAtServer': 'ПриСохраненииПользовательскихНастроекНаСервере',
    'OnLoadVariantAtServer': 'ПриЗагрузкеВариантаНаСервере', 'OnSaveVariantAtServer': 'ПриСохраненииВариантаНаСервере',
    'ChoiceProcessingAtServer': 'ОбработкаВыбораНаСервере',
    'RefreshRequestProcessing': 'ОбработкаЗапросаОбновления',
    'BeforeLoadVariantAtServer': 'ПередЗагрузкойВариантаНаСервере',
    'OnMainServerAvailabilityChange': 'ПриИзмененииДоступностиОсновногоСервера',
}
# Events not listed above are shown with their English name.

CALL_TYPES = {'Before': 'Перед', 'After': 'После', 'Override': 'Вместо'}


def form_event_ru(ev: str) -> str:
    return FORM_EVENTS.get(ev, ev)


# ----------------------------------------------------------------- types

_TYPE_SUFFIXES = ('RoutePointRef', 'RecordManager', 'RecordSet', 'RecordKey', 'Selection', 'Manager',
                  'Object', 'List', 'Ref')
_CFG_TYPE_RE = re.compile(r'^(\w+?)(' + '|'.join(_TYPE_SUFFIXES) + r')\.(.+)$')
_PRIM = {
    'xs:string': 'Строка', 'xs:decimal': 'Число', 'xs:boolean': 'Булево', 'xs:dateTime': 'Дата',
    'v8:UUID': 'УникальныйИдентификатор', 'v8:ValueStorage': 'ХранилищеЗначения', 'v8:Null': 'Null',
    'xs:base64Binary': 'ДвоичныеДанные', 'v8:StandardPeriod': 'СтандартныйПериод',
    'v8:StandardBeginningDate': 'СтандартнаяДатаНачала', 'v8:Type': 'Тип', 'v8:TypeDescription': 'ОписаниеТипов',
    'v8:FillChecking': 'ПроверкаЗаполнения', 'xs:anyURI': 'Строка', 'v8:AccountType': 'ВидСчета',
    'v8:AccumulationRecordType': 'ВидДвиженияНакопления', 'v8:AccountingRecordType': 'ВидДвиженияБухгалтерии',
}


def parse_cfg_type(t: str) -> str:
    """Convert one <v8:Type>/<v8:TypeSet> value to compact form.

    'ref:Справочник.Номенклатура'  reference type (also Object/RecordSet/...: 'obj:...', 'set:...')
    'def:ИмяОпределяемогоТипа'     defined type (to be expanded)
    'char:ИмяПВХ'                  characteristic value types of a ChartOfCharacteristicTypes
    'any:Справочник'               any reference of a kind (cfg:CatalogRef without name)
    'anyref'                       cfg:AnyRef
    'prim:Строка'                  primitive
    """
    if t.startswith('cfg:'):
        body = t[4:]
        if body.startswith('DefinedType.'):
            return 'def:' + body[12:]
        if body.startswith('Characteristic.'):
            return 'char:' + body[15:]
        if body == 'AnyRef':
            return 'anyref'
        m = _CFG_TYPE_RE.match(body)
        if m:
            en, suf, name = m.groups()
            ru = EN_TO_RU.get(en)
            if ru is None and suf == 'RoutePointRef':
                ru = 'БизнесПроцесс'
            if ru:
                kind = {'Ref': 'ref', 'RoutePointRef': 'ref'}.get(suf, 'obj')
                return f'{kind}:{ru}.{name}'
        for suf in _TYPE_SUFFIXES:          # cfg:CatalogRef  (any catalog)
            if body.endswith(suf) and EN_TO_RU.get(body[: -len(suf)]):
                return 'any:' + EN_TO_RU[body[: -len(suf)]]
        return 'other:' + body
    return 'prim:' + _PRIM.get(t, t.split(':', 1)[-1])


def parse_type_el(el) -> list[str]:
    """Parse a <Type> (or <Source>, <CommandParameterType>) element into compact type list."""
    if el is None:
        return []
    out = []
    quals = {}
    for c in el:
        tag = _local(c.tag)
        if tag in ('Type', 'TypeSet') and c.text:
            out.append(parse_cfg_type(c.text.strip()))
        elif tag == 'StringQualifiers':
            ln = c.findtext(_V8 + 'Length')
            if ln and ln != '0':
                quals['prim:Строка'] = f'prim:Строка({ln})'
        elif tag == 'NumberQualifiers':
            d = c.findtext(_V8 + 'Digits') or ''
            f = c.findtext(_V8 + 'FractionDigits') or '0'
            quals['prim:Число'] = f'prim:Число({d},{f})' if f != '0' else f'prim:Число({d})'
        elif tag == 'DateQualifiers':
            dp = c.findtext(_V8 + 'DateFractions')
            if dp == 'DateTime':
                quals['prim:Дата'] = 'prim:ДатаВремя'
            elif dp == 'Time':
                quals['prim:Дата'] = 'prim:Время'
    return [quals.get(t, t) for t in out]


def expand_types(types: list[str], defined: dict[str, list[str]],
                 characteristics: dict[str, list[str]] | None = None) -> list[str]:
    """Replace def:/char: entries by their contents (recursively, cycle-safe).

    Unknown defined types are kept as 'def:X' so the gap is visible."""
    out: list[str] = []
    seen: set[str] = set()
    expanded: set[str] = set()

    def add(ts):
        for t in ts:
            sub = None
            if t.startswith('def:'):
                sub = defined.get(t[4:])
            elif t.startswith('char:') and characteristics is not None:
                sub = characteristics.get(t[5:])
            if sub is not None:
                if t not in expanded:
                    expanded.add(t)
                    add(sub)
            elif t not in seen:
                seen.add(t)
                out.append(t)

    add(types)
    return out


def type_object(t: str) -> str | None:
    """'ref:Справочник.X' / 'obj:Документ.X' -> 'Справочник.X' (metadata object full name)."""
    if t.startswith(('ref:', 'obj:')):
        return t[4:]
    return None


def md_ref(s: str) -> str:
    """MDObjectRef 'AccumulationRegister.X' -> 'РегистрНакопления.X'."""
    s = s.strip()
    en, _, rest = s.partition('.')
    ru = EN_TO_RU.get(en)
    return f'{ru}.{rest}' if ru and rest else s


def method_ref(s: str) -> tuple[str, str] | None:
    """'CommonModule.Модуль.Метод' -> ('ОбщийМодуль.Модуль', 'Метод')."""
    parts = (s or '').strip().split('.')
    if len(parts) < 3:
        return None
    return md_ref('.'.join(parts[:-1])), parts[-1]


# ----------------------------------------------------------------- model

@dataclass
class ConfigInfo:
    name: str
    synonym: str
    version: str
    vendor: str
    compatibility: str
    is_extension: bool = False
    extension_purpose: str | None = None
    name_prefix: str | None = None


@dataclass
class MdAttr:
    name: str
    synonym: str
    kind: str                 # Реквизит | Измерение | Ресурс | РеквизитАдресации
    types: list[str]


@dataclass
class MdTabular:
    name: str
    synonym: str
    attributes: list[MdAttr]


@dataclass
class MdObject:
    md_type: str              # Russian singular
    name: str
    synonym: str
    comment: str
    uuid: str
    adopted: bool = False     # extension: borrowed object of the main configuration
    props: dict = field(default_factory=dict)
    attributes: list[MdAttr] = field(default_factory=list)
    tabular_sections: list[MdTabular] = field(default_factory=list)
    forms: list[str] = field(default_factory=list)
    commands: list[tuple[str, str]] = field(default_factory=list)   # (name, synonym)
    templates: list[str] = field(default_factory=list)
    enum_values: list[tuple[str, str]] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        return f'{self.md_type}.{self.name}'


@dataclass
class FormEvent:
    event: str                # English name as in XML
    handler: str
    call_type: str | None = None   # extension forms: Перед | После | Вместо


@dataclass
class FormItem:
    name: str
    item_type: str            # InputField, Button, Table, ...
    data_path: str | None
    events: list[FormEvent]
    command: str | None = None


@dataclass
class FormCommand:
    name: str
    actions: list[FormEvent]  # usually one (event='Action')


@dataclass
class FormElement:
    name: str
    item_type: str
    data_path: str | None
    parent: int               # index in FormInfo.elements, -1 for top level
    auxiliary: bool = False   # ExtendedTooltip, ContextMenu, search/status additions


@dataclass
class FormInfo:
    events: list[FormEvent]
    items: list[FormItem]     # only items that have event handlers
    commands: list[FormCommand]
    item_count: int = 0
    base_form: bool = False   # extension: form borrowed with BaseForm
    elements: list[FormElement] = field(default_factory=list)
    main_attribute: str | None = None          # usually Объект / Запись / Список
    main_attribute_types: list[str] = field(default_factory=list)
    attributes: list[str] = field(default_factory=list)   # names of form attributes


# ----------------------------------------------------------------- parsers

def _text(el, path, default=''):
    if el is None:
        return default
    v = el.findtext(path)
    return v.strip() if v else default


def _synonym(el) -> str:
    if el is None:
        return ''
    items = el.findall(_V8 + 'item')
    first = ''
    for it in items:
        content = it.findtext(_V8 + 'content') or ''
        if (it.findtext(_V8 + 'lang') or '') == 'ru':
            return content.strip()
        first = first or content.strip()
    return first


def _root(data: bytes):
    return ET.fromstring(data)


def parse_configuration(data: bytes) -> ConfigInfo:
    root = _root(data)
    cfg = root.find(_MD + 'Configuration')
    pr = cfg.find(_MD + 'Properties')
    purpose = _text(pr, _MD + 'ConfigurationExtensionPurpose') or None
    belonging = _text(pr, _MD + 'ObjectBelonging') or None
    return ConfigInfo(
        name=_text(pr, _MD + 'Name'),
        synonym=_synonym(pr.find(_MD + 'Synonym')),
        version=_text(pr, _MD + 'Version'),
        vendor=_text(pr, _MD + 'Vendor'),
        compatibility=_text(pr, _MD + 'CompatibilityMode'),
        is_extension=bool(purpose or belonging),
        extension_purpose=purpose,
        name_prefix=_text(pr, _MD + 'NamePrefix') or None,
    )


# Scalar properties worth keeping per metadata type (English XML names)
_KEEP_PROPS = {
    'CommonModule': ('Global', 'ClientManagedApplication', 'Server', 'ExternalConnection',
                     'ClientOrdinaryApplication', 'ServerCall', 'Privileged', 'ReturnValuesReuse'),
    'Document': ('Posting', 'RealTimePosting', 'RegisterRecordsDeletion', 'RegisterRecordsWritingOnPost'),
    'AccumulationRegister': ('RegisterType',),
    'InformationRegister': ('InformationRegisterPeriodicity', 'WriteMode'),
    'EventSubscription': ('Event',),
    'ScheduledJob': ('Use', 'Predefined', 'Key', 'RestartCountOnFailure'),
    'Catalog': ('Hierarchical',),
    'Constant': (),
}

_ATTR_KINDS = {'Attribute': 'Реквизит', 'Dimension': 'Измерение', 'Resource': 'Ресурс',
               'AddressingAttribute': 'РеквизитАдресации', 'AccountingFlag': 'ПризнакУчета',
               'ExtDimensionAccountingFlag': 'ПризнакУчетаСубконто'}
_BOOL = {'true': True, 'false': False}


def _attr(el, kind) -> MdAttr:
    pr = el.find(_MD + 'Properties')
    return MdAttr(name=_text(pr, _MD + 'Name'), synonym=_synonym(pr.find(_MD + 'Synonym')),
                  kind=kind, types=parse_type_el(pr.find(_MD + 'Type')))


def parse_object(data: bytes) -> MdObject:
    root = _root(data)
    obj = next(iter(root))
    en = _local(obj.tag)
    ru = EN_TO_RU.get(en, en)
    pr = obj.find(_MD + 'Properties')
    o = MdObject(md_type=ru, name=_text(pr, _MD + 'Name'), synonym=_synonym(pr.find(_MD + 'Synonym')),
                 comment=_text(pr, _MD + 'Comment'), uuid=obj.get('uuid', ''),
                 adopted=_text(pr, _MD + 'ObjectBelonging') == 'Adopted')
    props = o.props
    for p in _KEEP_PROPS.get(en, ()):
        v = pr.findtext(_MD + p)
        if v is not None:
            v = v.strip()
            props[p] = _BOOL.get(v, v)
    if en == 'Document':
        rr = pr.find(_MD + 'RegisterRecords')
        props['RegisterRecords'] = [md_ref(i.text) for i in rr] if rr is not None else []
        bo = pr.find(_MD + 'BasedOn')
        if bo is not None and len(bo):
            props['BasedOn'] = [md_ref(i.text) for i in bo]
    elif en == 'EventSubscription':
        props['Source'] = parse_type_el(pr.find(_MD + 'Source'))
        h = method_ref(_text(pr, _MD + 'Handler'))
        props['Handler'] = list(h) if h else None
        props['EventRu'] = SUBSCRIPTION_EVENTS.get(props.get('Event', ''), props.get('Event'))
    elif en == 'ScheduledJob':
        h = method_ref(_text(pr, _MD + 'MethodName'))
        props['MethodName'] = list(h) if h else None
    elif en in ('DefinedType', 'ChartOfCharacteristicTypes', 'Constant', 'SessionParameter',
                'FunctionalOption', 'CommonAttribute'):
        t = parse_type_el(pr.find(_MD + 'Type'))
        if t:
            props['Type'] = t
        if en == 'FunctionalOption':
            loc = _text(pr, _MD + 'Location')
            if loc:
                props['Location'] = md_ref(loc)
    elif en == 'ExchangePlan':
        props['DistributedInfoBase'] = _BOOL.get(_text(pr, _MD + 'DistributedInfoBase'), None)

    ch = obj.find(_MD + 'ChildObjects')
    if ch is not None:
        for c in ch:
            tag = _local(c.tag)
            if tag in _ATTR_KINDS:
                o.attributes.append(_attr(c, _ATTR_KINDS[tag]))
            elif tag == 'TabularSection':
                tpr = c.find(_MD + 'Properties')
                tch = c.find(_MD + 'ChildObjects')
                attrs = [_attr(a, 'Реквизит') for a in tch.findall(_MD + 'Attribute')] if tch is not None else []
                o.tabular_sections.append(MdTabular(_text(tpr, _MD + 'Name'),
                                                    _synonym(tpr.find(_MD + 'Synonym')), attrs))
            elif tag == 'Form':
                o.forms.append((c.text or '').strip())
            elif tag == 'Template':
                o.templates.append((c.text or '').strip())
            elif tag == 'Command':
                cpr = c.find(_MD + 'Properties')
                o.commands.append((_text(cpr, _MD + 'Name'), _synonym(cpr.find(_MD + 'Synonym'))))
            elif tag == 'EnumValue':
                vpr = c.find(_MD + 'Properties')
                o.enum_values.append((_text(vpr, _MD + 'Name'), _synonym(vpr.find(_MD + 'Synonym'))))
    return o


def parse_form_meta(data: bytes) -> dict:
    """Forms/<F>.xml: name, synonym, form type (Managed / Ordinary)."""
    root = _root(data)
    obj = next(iter(root))
    pr = obj.find(_MD + 'Properties')
    return {'name': _text(pr, _MD + 'Name'), 'synonym': _synonym(pr.find(_MD + 'Synonym')),
            'form_type': _text(pr, _MD + 'FormType') or 'Managed',
            'adopted': _text(pr, _MD + 'ObjectBelonging') == 'Adopted'}


def _events(el) -> list[FormEvent]:
    evs = el.find(_LF + 'Events')
    if evs is None:
        return []
    out = []
    for e in evs:
        if _local(e.tag) == 'Event' and e.text:
            ct = e.get('callType')
            out.append(FormEvent(e.get('name', ''), e.text.strip(), CALL_TYPES.get(ct, ct) if ct else None))
    return out


_FORM_ROOT_SKIP = {'Attributes', 'Commands', 'Parameters', 'CommandSet', 'Events', 'CommandInterface',
                   'BaseForm', 'Title', 'ConversationsRepresentation'}


AUXILIARY_ITEMS = {'ExtendedTooltip', 'ContextMenu', 'SearchStringAddition', 'ViewStatusAddition',
                   'SearchControlAddition'}
_WALK_SKIP = {'Events', 'Title', 'ToolTip', 'Picture', 'Font', 'Format', 'EditFormat', 'ChoiceList',
              'ChoiceParameters', 'ChoiceParameterLinks', 'TypeLink', 'InputHint', 'Mask'}


def parse_form(data: bytes) -> FormInfo:
    """Ext/Form.xml of a managed form."""
    root = _root(data)
    info = FormInfo(events=_events(root), items=[], commands=[])
    info.base_form = root.find(_LF + 'BaseForm') is not None

    def element(c, tag, parent):
        info.item_count += 1
        dp = _text(c, _LF + 'DataPath') or None
        info.elements.append(FormElement(c.get('name'), tag, dp, parent, tag in AUXILIARY_ITEMS))
        evs = _events(c)
        if evs:
            info.items.append(FormItem(c.get('name'), tag, dp, evs, _text(c, _LF + 'CommandName') or None))
        return len(info.elements) - 1

    def walk(el, parent):
        for c in el:
            tag = _local(c.tag)
            if tag in _WALK_SKIP:
                continue
            if c.get('name') is not None and c.get('id') is not None:
                walk(c, element(c, tag, parent))
            else:
                walk(c, parent)      # containers: ChildItems, ...

    for c in root:
        tag = _local(c.tag)
        if tag not in _FORM_ROOT_SKIP:
            if c.get('name') is not None and c.get('id') is not None:   # AutoCommandBar
                walk(c, element(c, tag, -1))
            else:
                walk(c, -1)

    attrs = root.find(_LF + 'Attributes')
    if attrs is not None:
        for a in attrs:
            if a.get('name'):
                info.attributes.append(a.get('name'))
            if info.main_attribute is None and (a.findtext(_LF + 'MainAttribute') or '').strip() == 'true':
                info.main_attribute = a.get('name')
                info.main_attribute_types = parse_type_el(a.find(_LF + 'Type'))

    cmds = root.find(_LF + 'Commands')
    if cmds is not None:
        for c in cmds:
            actions = []
            for a in c.findall(_LF + 'Action'):
                if a.text and a.text.strip():
                    ct = a.get('callType')
                    actions.append(FormEvent('Action', a.text.strip(), CALL_TYPES.get(ct, ct) if ct else None))
            info.commands.append(FormCommand(c.get('name', ''), actions))
    return info


# ------------------------------------------------------------------ data composition schemas

DCS_ROOT = b'DataCompositionSchema'
DATASET_KINDS = {'DataSetQuery': 'запрос', 'DataSetObject': 'объект', 'DataSetUnion': 'объединение'}


@dataclass
class DataSet:
    name: str
    kind: str                 # запрос | объект | объединение
    query: str | None = None
    union: str | None = None  # name of the enclosing union data set


def parse_dcs(data: bytes) -> list[DataSet]:
    """Data sets of a data composition schema (Template.xml of a DataCompositionSchema template)."""
    root = ET.fromstring(data)
    out: list[DataSet] = []

    def walk(el, union=None):
        typ = el.get('{%s}type' % NS_XSI, '').split(':')[-1]
        name = next((c.text or '' for c in el if _local(c.tag) == 'name'), '').strip()
        kind = DATASET_KINDS.get(typ, typ or '?')
        q = next((c.text for c in el if _local(c.tag) == 'query'), None) if typ == 'DataSetQuery' else None
        out.append(DataSet(name or f'НаборДанных{len(out) + 1}', kind, q, union))
        if typ == 'DataSetUnion':
            for c in el:
                if _local(c.tag) == 'item':
                    walk(c, name)

    for el in root:
        if _local(el.tag) == 'dataSet':
            walk(el)
    return out