"""Upload web UI (FastAPI): one HTML page + small JSON API. No authentication; bind to 127.0.0.1."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import anyio
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from . import db
from .service import IndexService, UserError

STATIC = Path(__file__).parent / 'static'


def build(svc: IndexService, db_path: str) -> FastAPI:
    app = FastAPI(title='onec-code upload', docs_url=None, redoc_url=None, openapi_url=None)

    def conn():
        return db.connect(db_path)

    @app.exception_handler(UserError)
    async def user_error(_req, ex):
        return JSONResponse({'error': str(ex)}, status_code=400)

    @app.get('/')
    async def index():
        return FileResponse(STATIC / 'index.html', headers={'Cache-Control': 'no-store'})

    @app.get('/api/snapshots')
    async def snapshots():
        def work():
            c = conn()
            try:
                rows = c.execute('SELECT * FROM snapshots ORDER BY created_at DESC, id DESC').fetchall()
            finally:
                c.close()
            st = svc.status()
            cur = st['current']
            queued = {q['label'] for q in st['queue']}
            out = []
            for r in rows:
                stats = json.loads(r['stats']) if r['stats'] else {}
                item = {k: r[k] for k in ('id', 'label', 'origin', 'config_name', 'config_version', 'created_at',
                                          'indexed_at', 'source_size', 'status', 'error', 'is_extension')}
                item['source_exists'] = os.path.exists(r['source_path'])
                item['modules'] = stats.get('modules')
                item['methods'] = stats.get('methods')
                item['delete_after'] = bool(r['delete_source_after'])
                if cur and cur['sid'] == r['id']:
                    item['status'] = 'deleting' if cur['job'] == 'delete' else 'indexing'
                    item['progress'] = cur.get('progress') or {}
                elif r['label'] in queued:
                    item['status'] = 'queued' if r['status'] != 'ready' else 'ready+queued'
                out.append(item)
            return {'snapshots': out, 'notes': st['notes'], 'busy': bool(cur or st['queue'])}
        return await anyio.to_thread.run_sync(work)

    @app.get('/api/check')
    async def check(title: str):
        await anyio.to_thread.run_sync(svc.check_title, title)
        return {'ok': True}

    @app.post('/api/upload')
    async def upload(request: Request, title: str, delete_after: bool = False):
        """Raw request body = zip file. Written to disk chunk by chunk (no size limit, no buffering)."""
        await anyio.to_thread.run_sync(svc.check_title, title)
        part = svc.new_upload_path()
        size = 0
        t0 = time.time()
        try:
            f = await anyio.to_thread.run_sync(lambda: open(part, 'wb'))
            try:
                async for chunk in request.stream():
                    if chunk:
                        size += len(chunk)
                        await anyio.to_thread.run_sync(f.write, chunk)
            finally:
                await anyio.to_thread.run_sync(f.close)
        except Exception:
            if os.path.exists(part):
                os.remove(part)
            raise
        if size == 0:
            os.remove(part)
            raise UserError('Пустой файл')
        sid = await anyio.to_thread.run_sync(svc.register_upload, title, part, delete_after)
        return {'id': sid, 'size': size, 'seconds': round(time.time() - t0, 1)}

    @app.post('/api/snapshots/{sid}/rename')
    async def rename(sid: int, request: Request):
        try:
            body = await request.json()
            label = body.get('label', '')
        except (ValueError, AttributeError):
            raise UserError('Ожидается JSON {"label": "..."} в UTF-8') from None
        await anyio.to_thread.run_sync(svc.rename, sid, label)
        return {'ok': True}

    @app.post('/api/snapshots/{sid}/reindex')
    async def reindex(sid: int, full: bool = False):
        svc.reindex(sid, full)
        return {'ok': True}

    @app.delete('/api/snapshots/{sid}')
    async def delete(sid: int):
        svc.delete(sid)
        return {'ok': True}

    @app.get('/api/status')
    async def status():
        return await anyio.to_thread.run_sync(svc.status)

    @app.exception_handler(HTTPException)
    async def http_error(_req, ex):
        return JSONResponse({'error': str(ex.detail)}, status_code=ex.status_code)

    return app
