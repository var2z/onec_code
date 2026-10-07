"""FastMCP server: tool registration. Tools run in worker threads with thread-local connections."""
from __future__ import annotations

import logging
import threading
from typing import Annotated, Literal

import anyio
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from . import db, tools
from .query import NotFound

log = logging.getLogger('onec.mcp')

INSTRUCTIONS = """Код и метаданные конфигураций 1С (выгрузки в файлы), граф связей.
config: метка снимка (без учёта регистра), имя конфигурации (последняя версия) или имя@версия; без config — поиск по всем снимкам.
Имена: 'Документ.РеализацияТоваровУслуг', 'Document.X.ManagerModule', 'ОбщийМодуль.ОбщегоНазначения' или просто 'ОбщегоНазначения',
'Документ.X.МодульОбъекта', 'Документ.X.Форма.ФормаДокумента', 'Документ.X.Товары.Номенклатура', синоним вместо имени.
Схемы компоновки (СКД) индексируются как модули: 'Отчет.X.Макет.ОсновнаяСхемаКомпоновкиДанных' (get_module — запросы,
get_module_outline — наборы данных, get_procedure — запрос одного набора); search_code ищет и по запросам СКД.
Confidence связей: exact | heuristic (~) | unresolved (? динамические вызовы) | broken (✗ цель отсутствует).
Тексты кода — только get_procedure / get_module; остальные инструменты возвращают имена и счётчики."""

_local = threading.local()
DB_PATH = None


def _conn():
    c = getattr(_local, 'conn', None)
    if c is None:
        c = _local.conn = db.connect(DB_PATH)
    return c


async def _run(fn, *args, **kw) -> str:
    def work():
        try:
            return fn(_conn(), *args, **kw)
        except NotFound as e:
            return f'Не найдено: {e}\n'
        except Exception as e:  # noqa: BLE001 - reported to the agent
            log.exception('tool %s failed', fn.__name__)
            return f'Ошибка: {type(e).__name__}: {e}\n'
    return await anyio.to_thread.run_sync(work)


Config = Annotated[str | None, Field(description='Снимок: метка, имя конфигурации или имя@версия; пусто — все')]
Limit = Annotated[int, Field(ge=1, le=1000)]


def build(db_path: str) -> FastMCP:
    global DB_PATH
    DB_PATH = db_path
    mcp = FastMCP('onec-code', instructions=INSTRUCTIONS, host='0.0.0.0', port=8002, streamable_http_path='/mcp',
                  log_level='WARNING')

    # ---------------- catalog
    @mcp.tool()
    async def list_configs() -> str:
        """Снимки конфигураций, сгруппированные по имени: метка, версия, дата, число объектов/модулей/методов."""
        return await _run(tools.list_configs)

    @mcp.tool()
    async def list_objects(query: Annotated[str | None, Field(description='Часть имени или синонима')] = None,
                           object_type: Annotated[str | None, Field(description='Документ/Document, Справочник, '
                                                                    'РегистрНакопления, ОбщийМодуль …')] = None,
                           config: Config = None, limit: Limit = 50, offset: int = 0) -> str:
        """Поиск объектов метаданных по имени/синониму."""
        return await _run(tools.list_objects, query, object_type, config, limit, offset)

    @mcp.tool()
    async def get_object(full_name: Annotated[str, Field(description='Напр. Документ.РеализацияТоваровУслуг')],
                         config: Config = None,
                         section: Annotated[str | None, Field(description='Имя табличной части (её реквизиты с '
                                                              'типами) или all')] = None) -> str:
        """Структура объекта: реквизиты с типами, табличные части, формы, модули, движения, подписки на него."""
        return await _run(tools.get_object, full_name, config, section)

    # ---------------- code
    @mcp.tool()
    async def search_code(query: Annotated[str, Field(description='Слова/фраза; допускается синтаксис FTS5 '
                                                               '(AND, OR, NOT, "фраза", префикс*)')],
                          config: Config = None, object_type: str | None = None,
                          limit: Limit = 20, offset: int = 0) -> str:
        """Полнотекстовый поиск по коду методов: модуль, метод, строка, фрагмент."""
        return await _run(tools.search_code, query, config, object_type, limit, offset)

    @mcp.tool()
    async def find_procedure(name: Annotated[str, Field(description='Имя метода (можно Модуль.Метод)')],
                             config: Config = None,
                             module_hint: Annotated[str | None, Field(description='Часть имени модуля или '
                                                                      'синонима объекта')] = None,
                             limit: Limit = 30) -> str:
        """Поиск метода по имени (точно → по началу → по части) во всех модулях: сигнатура, Экспорт, директива, строки."""
        return await _run(tools.find_procedure, name, config, module_hint, limit)

    @mcp.tool()
    async def get_procedure(module: Annotated[str, Field(description='Модуль: ОбщийМодуль.X, Документ.X.МодульОбъекта, '
                                                                  'Документ.X.Форма.Ф …')],
                            name: str, config: Config = None,
                            max_lines: Annotated[int, Field(ge=20, le=2000, description='Длинный метод обрезается; '
                                                            'продолжение — get_module(start_line, end_line)')] = 300
                            ) -> str:
        """Текст одного метода с номерами строк (длинные методы — первые max_lines строк)."""
        return await _run(tools.get_procedure, module, name, config, max_lines)

    @mcp.tool()
    async def get_module_outline(module: str, config: Config = None, limit: Limit = 150, offset: int = 0) -> str:
        """Список методов модуля: строки, сигнатура, Экспорт, директива, область."""
        return await _run(tools.get_module_outline, module, config, limit, offset)

    @mcp.tool()
    async def get_module(module: str, config: Config = None, start_line: int | None = None,
                         end_line: int | None = None) -> str:
        """Текст модуля кусками (не более 400 строк за вызов)."""
        return await _run(tools.get_module, module, config, start_line, end_line)

    # ---------------- graph
    @mcp.tool()
    async def get_dependencies(node: Annotated[str, Field(description='Объект, реквизит, модуль, метод или форма')],
                               direction: Literal['out', 'in', 'both'] = 'out',
                               edge_kinds: Annotated[list[str] | None, Field(
                                   description='calls, handler, event_subscription, scheduled_job, form_handler, '
                                               'form_attr, attr_type, register_records, writes_register, reads_table, '
                                               'posting_mechanism, extension_intercepts')] = None,
                               depth: Annotated[int, Field(ge=1, le=5)] = 1,
                               config: Config = None, limit: Limit = 200) -> str:
        """Связи узла графа (только имена, виды связей, confidence, счётчики)."""
        return await _run(tools.get_dependencies, node, direction, edge_kinds, depth, config, limit)

    @mcp.tool()
    async def find_callers(name: Annotated[str, Field(description='Метод (можно Модуль.Метод)')],
                           module: str | None = None, config: Config = None,
                           depth: Annotated[int, Field(ge=1, le=5)] = 1, limit: Limit = 100) -> str:
        """Кто вызывает метод (вызовы, подписки, обработчики форм, регл. задания, перехваты расширений)."""
        return await _run(tools.find_callers, name, module, config, depth, limit)

    @mcp.tool()
    async def find_calls(module: str, name: str, config: Config = None) -> str:
        """Что вызывает метод: вызовы с confidence, запись/чтение регистров и таблиц, динамические вызовы."""
        return await _run(tools.find_calls, module, name, config)

    @mcp.tool()
    async def who_writes(register: Annotated[str, Field(description='Напр. РегистрНакопления.ТоварыНаСкладах')],
                         config: Config = None, limit: Limit = 100) -> str:
        """Кто пишет регистр: документы (RegisterRecords), методы по коду, механизмы проведения."""
        return await _run(tools.who_writes, register, config, limit)

    @mcp.tool()
    async def who_reads(object_or_register: str, config: Config = None, limit: Limit = 100,
                        source: Annotated[str | None, Field(description='code — только код | dcs — только схемы '
                                                                         'СКД (отчёты и др.) | пусто — всё')] = None
                        ) -> str:
        """Кто читает объект/регистр в запросах (эвристика), с виртуальными таблицами: методы в коде и наборы
        данных схем компоновки (СКД) отчётов, обработок и др."""
        return await _run(tools.who_reads, object_or_register, config, limit, source)

    @mcp.tool()
    async def on_write_or_post(document: str,
                               event: Annotated[str, Field(description='post (проведение, по умолчанию) | write | '
                                                           'конкретное событие: ПередЗаписью/BeforeWrite …')] = 'post',
                               depth: Annotated[int, Field(ge=0, le=5)] = 2, config: Config = None,
                               verbose: bool = False) -> str:
        """Что выполнится при записи/проведении документа: обработчики модуля объекта, подписки, механизмы
        проведения, перехваты расширений, наборы записей регистров, вызовы вглубь."""
        return await _run(tools.on_write_or_post, document, event, depth, config, verbose)

    @mcp.tool()
    async def impact(node: str, depth: Annotated[int, Field(ge=1, le=5)] = 2, config: Config = None) -> str:
        """Обратный обход: что затронет изменение объекта/реквизита/метода/модуля."""
        return await _run(tools.impact, node, depth, config)

    @mcp.tool()
    async def call_path(from_node: Annotated[str, Field(description='Метод, команда/элемент формы '
                                                                    '(Документ.X.Форма.Ф.Команды.Имя), документ …')],
                        to_node: Annotated[str, Field(description='Метод, регистр, документ …')],
                        max_depth: Annotated[int, Field(ge=1, le=12)] = 6, config: Config = None,
                        edge_kinds: Annotated[list[str] | None, Field(
                            description='По умолчанию ["calls"]. Путь «кнопка → регистр»: ["form_handler","calls",'
                                        '"posting_mechanism","writes_register"]; также event_subscription, '
                                        'scheduled_job, handler')] = None) -> str:
        """Кратчайшие пути между двумя узлами графа (по умолчанию — только вызовы)."""
        return await _run(tools.call_path, from_node, to_node, max_depth, config, edge_kinds)

    @mcp.tool()
    async def find_broken_refs(config: Config = None,
                               kind: Annotated[str | None, Field(description='calls | form_handler | handler | '
                                                                 'scheduled_job | extension_intercepts …')] = None,
                               limit: Limit = 100, offset: int = 0) -> str:
        """Битые ссылки: обработчики форм/команд, подписок, регл. заданий и вызовы ОбщийМодуль("X") на
        несуществующий модуль или метод. Без kind вызовы ОбщийМодуль("X") на отсутствующие модули
        (необязательные подсистемы библиотек) свёрнуты в сводку по модулям; kind=calls — все подробно.
        Вызовы модулей без исходных текстов (Module.bin) — не битые, а unresolved."""
        return await _run(tools.find_broken_refs, config, kind, limit, offset)

    @mcp.tool()
    async def find_form_usages(attribute: Annotated[str, Field(description='Документ.X.Реквизит или '
                                                                           'Документ.X.ТЧ.Реквизит')],
                               config: Config = None) -> str:
        """На каких формах выводится реквизит (через основной реквизит формы)."""
        return await _run(tools.find_form_usages, attribute, config)

    @mcp.tool()
    async def get_form_element(form: Annotated[str, Field(description='Документ.X.Форма.ФормаДокумента')],
                               element: str, config: Config = None) -> str:
        """Элемент формы: тип, путь данных, родители, реквизит объекта, обработчики."""
        return await _run(tools.get_form_element, form, element, config)

    # ---------------- versions
    @mcp.tool()
    async def diff_procedure(module: str, name: str, config_a: str, config_b: str) -> str:
        """Unified diff одного метода между двумя снимками."""
        return await _run(tools.diff_procedure, module, name, config_a, config_b)

    @mcp.tool()
    async def diff_module(module: str, config_a: str, config_b: str) -> str:
        """Какие методы модуля добавлены/удалены/изменены (без текста)."""
        return await _run(tools.diff_module, module, config_a, config_b)

    @mcp.tool()
    async def diff_configs(config_a: str, config_b: str,
                           object_filter: Annotated[str | None, Field(description='Тип (Документ) или часть имени')] = None,
                           limit: Limit = 100, offset: int = 0) -> str:
        """Изменённые модули, формы и объекты метаданных (добавленные/удалённые реквизиты и т.п.)."""
        return await _run(tools.diff_configs, config_a, config_b, object_filter, limit, offset)

    # ---------------- service
    @mcp.tool()
    async def reindex(config: Config = None, full: bool = False) -> str:
        """Переиндексация: инкрементальная (по умолчанию) или полная; также подхватывает изменения inbox."""
        return await _run(tools.reindex, config, full)

    @mcp.tool()
    async def index_status() -> str:
        """Идёт ли индексация, этап и прогресс, очередь, ошибки."""
        return await _run(tools.index_status)

    return mcp
