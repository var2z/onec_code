"""Layout of a configuration dump (zip from the Designer or DumpConfigToFiles folder).

Maps metadata type names between the XML folder / English / Russian forms and
classifies dump paths into the entries the indexer needs.
"""
from __future__ import annotations

from dataclasses import dataclass

# folder, English (XML root tag), Russian singular, Russian manager collection, English collection
TYPES = [
    ('Catalogs', 'Catalog', 'Справочник', 'Справочники', 'Catalogs'),
    ('Documents', 'Document', 'Документ', 'Документы', 'Documents'),
    ('DocumentJournals', 'DocumentJournal', 'ЖурналДокументов', 'ЖурналыДокументов', 'DocumentJournals'),
    ('Enums', 'Enum', 'Перечисление', 'Перечисления', 'Enums'),
    ('Reports', 'Report', 'Отчет', 'Отчеты', 'Reports'),
    ('DataProcessors', 'DataProcessor', 'Обработка', 'Обработки', 'DataProcessors'),
    ('ChartsOfCharacteristicTypes', 'ChartOfCharacteristicTypes', 'ПланВидовХарактеристик',
     'ПланыВидовХарактеристик', 'ChartsOfCharacteristicTypes'),
    ('ChartsOfAccounts', 'ChartOfAccounts', 'ПланСчетов', 'ПланыСчетов', 'ChartsOfAccounts'),
    ('ChartsOfCalculationTypes', 'ChartOfCalculationTypes', 'ПланВидовРасчета', 'ПланыВидовРасчета',
     'ChartsOfCalculationTypes'),
    ('InformationRegisters', 'InformationRegister', 'РегистрСведений', 'РегистрыСведений', 'InformationRegisters'),
    ('AccumulationRegisters', 'AccumulationRegister', 'РегистрНакопления', 'РегистрыНакопления',
     'AccumulationRegisters'),
    ('AccountingRegisters', 'AccountingRegister', 'РегистрБухгалтерии', 'РегистрыБухгалтерии', 'AccountingRegisters'),
    ('CalculationRegisters', 'CalculationRegister', 'РегистрРасчета', 'РегистрыРасчета', 'CalculationRegisters'),
    ('BusinessProcesses', 'BusinessProcess', 'БизнесПроцесс', 'БизнесПроцессы', 'BusinessProcesses'),
    ('Tasks', 'Task', 'Задача', 'Задачи', 'Tasks'),
    ('ExchangePlans', 'ExchangePlan', 'ПланОбмена', 'ПланыОбмена', 'ExchangePlans'),
    ('Constants', 'Constant', 'Константа', 'Константы', 'Constants'),
    ('Sequences', 'Sequence', 'Последовательность', 'Последовательности', 'Sequences'),
    ('SettingsStorages', 'SettingsStorage', 'ХранилищеНастроек', 'ХранилищаНастроек', 'SettingsStorages'),
    ('ExternalDataSources', 'ExternalDataSource', 'ВнешнийИсточникДанных', 'ВнешниеИсточникиДанных',
     'ExternalDataSources'),
    ('CommonModules', 'CommonModule', 'ОбщийМодуль', None, None),
    ('CommonForms', 'CommonForm', 'ОбщаяФорма', None, None),
    ('CommonCommands', 'CommonCommand', 'ОбщаяКоманда', None, None),
    ('CommandGroups', 'CommandGroup', 'ГруппаКоманд', None, None),
    ('CommonTemplates', 'CommonTemplate', 'ОбщийМакет', None, None),
    ('CommonPictures', 'CommonPicture', 'ОбщаяКартинка', None, None),
    ('CommonAttributes', 'CommonAttribute', 'ОбщийРеквизит', None, None),
    ('SessionParameters', 'SessionParameter', 'ПараметрСеанса', None, None),
    ('Roles', 'Role', 'Роль', None, None),
    ('Subsystems', 'Subsystem', 'Подсистема', None, None),
    ('StyleItems', 'StyleItem', 'ЭлементСтиля', None, None),
    ('Styles', 'Style', 'Стиль', None, None),
    ('Languages', 'Language', 'Язык', None, None),
    ('FilterCriteria', 'FilterCriterion', 'КритерийОтбора', None, None),
    ('EventSubscriptions', 'EventSubscription', 'ПодпискаНаСобытие', None, None),
    ('ScheduledJobs', 'ScheduledJob', 'РегламентноеЗадание', None, None),
    ('FunctionalOptions', 'FunctionalOption', 'ФункциональнаяОпция', None, None),
    ('FunctionalOptionsParameters', 'FunctionalOptionsParameter', 'ПараметрФункциональныхОпций', None, None),
    ('DefinedTypes', 'DefinedType', 'ОпределяемыйТип', None, None),
    ('XDTOPackages', 'XDTOPackage', 'ПакетXDTO', None, None),
    ('WebServices', 'WebService', 'WebСервис', None, None),
    ('HTTPServices', 'HTTPService', 'HTTPСервис', None, None),
    ('WSReferences', 'WSReference', 'WSСсылка', None, None),
    ('IntegrationServices', 'IntegrationService', 'СервисИнтеграции', None, None),
    ('Bots', 'Bot', 'Бот', None, None),
    ('DocumentNumerators', 'DocumentNumerator', 'НумераторДокументов', None, None),
    ('Interfaces', 'Interface', 'Интерфейс', None, None),
]

FOLDER_TO_RU = {t[0]: t[2] for t in TYPES}
EN_TO_RU = {t[1]: t[2] for t in TYPES}
RU_TO_EN = {t[2]: t[1] for t in TYPES}
RU_TO_FOLDER = {t[2]: t[0] for t in TYPES}

# Any spelling (lowercase) -> Russian singular type. Includes folder names, manager
# collections (Документы/Documents) and a few common aliases.
TYPE_ALIASES: dict[str, str] = {}
for _f, _en, _ru, _ru_pl, _en_pl in TYPES:
    for _a in (_f, _en, _ru, _ru_pl, _en_pl):
        if _a:
            TYPE_ALIASES[_a.lower()] = _ru
TYPE_ALIASES.update({
    'конфигурация': 'Конфигурация', 'configuration': 'Конфигурация',
    'пвх': 'ПланВидовХарактеристик', 'рн': 'РегистрНакопления', 'рс': 'РегистрСведений',
    'общиемодули': 'ОбщийМодуль', 'общиеформы': 'ОбщаяФорма', 'общиекоманды': 'ОбщаяКоманда',
    'подпискинасобытия': 'ПодпискаНаСобытие', 'регламентныезадания': 'РегламентноеЗадание',
    'определяемыетипы': 'ОпределяемыйТип', 'webservice': 'WebСервис', 'httpservice': 'HTTPСервис',
})

# Manager collections used in code: Документы.X.Метод()
MANAGER_COLLECTIONS = {a.lower(): ru for _f, _en, ru, rpl, epl in TYPES for a in (rpl, epl) if a}

REGISTER_TYPES = {'РегистрНакопления', 'РегистрСведений', 'РегистрБухгалтерии', 'РегистрРасчета'}

# Types with "Ext/..." object/manager modules etc.
MODULE_FILES = {
    'ObjectModule.bsl': 'МодульОбъекта',
    'ManagerModule.bsl': 'МодульМенеджера',
    'RecordSetModule.bsl': 'МодульНабораЗаписей',
    'ValueManagerModule.bsl': 'МодульМенеджераЗначения',
    'Module.bsl': 'Модуль',                 # common module, web/http service, bot ...
    'CommandModule.bsl': 'МодульКоманды',
}
# Modules shipped without source text (password-protected / "без исходных текстов"): Module.bin ...
BIN_MODULE_FILES = {k[:-4] + '.bin': v for k, v in MODULE_FILES.items()}
CONFIG_MODULES = {
    'ManagedApplicationModule.bsl': 'МодульУправляемогоПриложения',
    'OrdinaryApplicationModule.bsl': 'МодульОбычногоПриложения',
    'SessionModule.bsl': 'МодульСеанса',
    'ExternalConnectionModule.bsl': 'МодульВнешнегоСоединения',
}
# Short module names accepted from users (lowercase) -> canonical
MODULE_KIND_ALIASES = {v.lower(): v for v in list(MODULE_FILES.values()) + list(CONFIG_MODULES.values())}
MODULE_KIND_ALIASES.update({
    'objectmodule': 'МодульОбъекта', 'managermodule': 'МодульМенеджера',
    'recordsetmodule': 'МодульНабораЗаписей', 'valuemanagermodule': 'МодульМенеджераЗначения',
    'module': 'Модуль', 'commandmodule': 'МодульКоманды',
    'managedapplicationmodule': 'МодульУправляемогоПриложения',
    'ordinaryapplicationmodule': 'МодульОбычногоПриложения',
    'sessionmodule': 'МодульСеанса', 'externalconnectionmodule': 'МодульВнешнегоСоединения',
})

CONFIG_OWNER = 'Конфигурация'


@dataclass(frozen=True)
class Entry:
    """A dump file the indexer is interested in.

    kind: config | object | form_meta | form | module
    owner: full name of the owning object ('Документ.X', 'Документ.X.Форма.F',
           'Документ.X.Команда.C', 'ОбщаяФорма.X', 'Конфигурация')
    module_kind: for modules (МодульОбъекта, Модуль, ...)
    """
    kind: str
    path: str
    md_type: str | None = None
    name: str | None = None
    owner: str | None = None
    module_kind: str | None = None
    sub: str | None = None       # form / command name
    nosrc: bool = False          # module without source text (*.bin)

    @property
    def module_name(self) -> str:
        if self.kind == 'template':     # data composition schema, indexed like a module of data sets
            return f'{self.owner}.Макет.{self.sub}' if self.sub else f'{self.owner}.СКД'
        return f'{self.owner}.{self.module_kind}'


def classify(path: str) -> Entry | None:
    p = path.split('/')
    n = len(p)
    if n == 1:
        return Entry('config', path) if p[0] == 'Configuration.xml' else None
    if n == 2 and p[0] == 'Ext' and p[1] in CONFIG_MODULES:
        return Entry('module', path, owner=CONFIG_OWNER, module_kind=CONFIG_MODULES[p[1]])
    t = FOLDER_TO_RU.get(p[0])
    if t is None:
        return None
    if n == 2:
        if p[1].endswith('.xml'):
            name = p[1][:-4]
            return Entry('object', path, t, name, owner=f'{t}.{name}')
        return None
    name = p[1]
    obj = f'{t}.{name}'
    if n == 4 and p[2] == 'Ext':
        mk = MODULE_FILES.get(p[3])
        if mk:
            return Entry('module', path, t, name, owner=obj, module_kind=mk)
        mk = BIN_MODULE_FILES.get(p[3])
        if mk:
            return Entry('module', path, t, name, owner=obj, module_kind=mk, nosrc=True)
        if p[3] == 'Form.xml' and t == 'ОбщаяФорма':
            return Entry('form', path, t, name, owner=obj)
        if p[3] == 'Template.xml' and t == 'ОбщийМакет':
            return Entry('template', path, t, name, owner=obj)
        return None
    if n == 6 and p[2] == 'Templates' and p[4:] == ['Ext', 'Template.xml']:
        # only data composition schemas are indexed; the indexer checks the file head
        return Entry('template', path, t, name, owner=obj, sub=p[3])
    if n == 5 and t == 'ОбщаяФорма' and p[2:4] == ['Ext', 'Form'] and p[4] in ('Module.bsl', 'Module.bin'):
        return Entry('module', path, t, name, owner=obj, module_kind='Модуль', nosrc=p[4] == 'Module.bin')
    if n >= 4 and p[2] == 'Forms':
        if n == 4 and p[3].endswith('.xml'):
            f = p[3][:-4]
            return Entry('form_meta', path, t, name, owner=f'{obj}.Форма.{f}', sub=f)
        f = p[3]
        if n == 6 and p[4:] == ['Ext', 'Form.xml']:
            return Entry('form', path, t, name, owner=f'{obj}.Форма.{f}', sub=f)
        if n == 7 and p[4:6] == ['Ext', 'Form'] and p[6] in ('Module.bsl', 'Module.bin'):
            return Entry('module', path, t, name, owner=f'{obj}.Форма.{f}', module_kind='Модуль', sub=f,
                         nosrc=p[6] == 'Module.bin')
        return None
    if n == 6 and p[2] == 'Commands' and p[4] == 'Ext' and p[5] in ('CommandModule.bsl', 'CommandModule.bin'):
        c = p[3]
        return Entry('module', path, t, name, owner=f'{obj}.Команда.{c}', module_kind='МодульКоманды', sub=c,
                     nosrc=p[5] == 'CommandModule.bin')
    return None
