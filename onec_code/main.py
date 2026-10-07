"""Entry point: indexing service + MCP (streamable HTTP, :8002/mcp) + upload web UI (:8003) in one process.

Environment:
  ONEC_DB            index database (default /data/index.db)
  ONEC_INBOX         read-only folder with zip dumps (default /inbox)
  ONEC_ARCHIVES      uploaded archives (default /archives)
  ONEC_WORKERS       parser processes (default: CPU count - 1, max 8)
  ONEC_INBOX_SCAN    inbox rescan period, seconds (default 60, 0 = only at start / reindex)
  MCP_PORT, WEB_PORT (8002, 8003)
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal

import uvicorn

from . import mcp_server, tools, web
from .service import IndexService


class _Server(uvicorn.Server):
    """Two servers share one loop: signals are handled once, in main()."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield

    def install_signal_handlers(self):  # uvicorn < 0.29
        pass


async def main():
    logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO'),
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    db_path = os.environ.get('ONEC_DB', '/data/index.db')
    workers = int(os.environ['ONEC_WORKERS']) if os.environ.get('ONEC_WORKERS') else None
    svc = IndexService(db_path, os.environ.get('ONEC_INBOX', '/inbox'), os.environ.get('ONEC_ARCHIVES', '/archives'),
                       workers, int(os.environ.get('ONEC_INBOX_SCAN', '60')))
    svc.start()
    tools.SERVICE = svc

    mcp = mcp_server.build(db_path)
    servers = [
        _Server(uvicorn.Config(mcp.streamable_http_app(), host='0.0.0.0', port=int(os.environ.get('MCP_PORT', 8002)),
                               log_level='warning', lifespan='on')),
        _Server(uvicorn.Config(web.build(svc, db_path), host='0.0.0.0', port=int(os.environ.get('WEB_PORT', 8003)),
                               log_level='warning')),
    ]
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, lambda: [setattr(s, 'should_exit', True) for s in servers])
    logging.getLogger('onec').info('MCP: http://0.0.0.0:8002/mcp, web: http://0.0.0.0:8003')
    await asyncio.gather(*(s.serve() for s in servers))


if __name__ == '__main__':
    asyncio.run(main())
