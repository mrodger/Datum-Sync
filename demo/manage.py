#!/usr/bin/env python3
"""Manage only this checkout's loopback prototype and private PostgreSQL cluster."""
import argparse
import base64
import asyncio
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.runtime'
TOOLS = Path.home() / '.local/share/datum-tools/root'
PYTHON = ROOT / '.venv/bin/python'
if not PYTHON.exists():
    PYTHON = Path(sys.executable)
WORKER = ROOT / 'worker'
APP = ROOT
PG = TOOLS / 'usr/lib/postgresql/16/bin'


def ensure_config():
    cfg = RUNTIME/'config.json'
    if not cfg.exists():
        return
    data = json.loads(cfg.read_text())
    changed=False
    for key in ('LEGACY_MCP_TOKEN','OFFICE_MCP_TOKEN','POSTGRES_MCP_TOKEN','HARNESS_MCP_TOKEN'):
        if key not in data:
            data[key] = secrets.token_urlsafe(32)
            changed=True
    if changed:
        cfg.write_text(json.dumps(data, indent=2))
        cfg.chmod(0o600)


def environment():
    env = dict(os.environ)
    env['LD_LIBRARY_PATH'] = str(TOOLS / 'usr/lib/x86_64-linux-gnu')
    env['PYTHONPATH'] = str(APP)
    env['PROJ_DATA'] = str(TOOLS / 'usr/share/proj')
    if (RUNTIME / 'config.json').exists():
        env.update(json.loads((RUNTIME / 'config.json').read_text()))
    return env


def pg(command, *args, **kwargs):
    return subprocess.run([str(PG / command), *map(str, args)], env=environment(), check=True, **kwargs)


def init():
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    RUNTIME.chmod(0o700)
    (RUNTIME / 'socket').mkdir(mode=0o700, exist_ok=True)
    cfg = RUNTIME / 'config.json'
    if not cfg.exists():
        secret = secrets.token_urlsafe(32)
        data = {'DATABASE_URL': f'postgresql://datum_portal:{secret}@127.0.0.1:55435/datum_portal',
                'PUBLIC_URL': 'http://127.0.0.1:8210', 'HOST': '127.0.0.1', 'PORT': '8210',
                'DATUM_SYNC_AUTH': 'on', 'PORTAL_LOCAL': '1',
                'DATUM_SYNC_SECRET_KEY_01': base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(), 'DATUM_SYNC_SECRET_KEY_CURRENT': '1',
                'DATUM_PG_PASSWORD': secret, 'LEGACY_MCP_TOKEN': secrets.token_urlsafe(32),
                'OFFICE_MCP_TOKEN': secrets.token_urlsafe(32), 'POSTGRES_MCP_TOKEN': secrets.token_urlsafe(32),
                'HARNESS_MCP_TOKEN': secrets.token_urlsafe(32)}
        cfg.write_text(json.dumps(data, indent=2))
        cfg.chmod(0o600)
    ensure_config()
    if not (RUNTIME / 'pgdata/PG_VERSION').exists():
        pg('initdb', '-D', RUNTIME / 'pgdata', '-L', TOOLS / 'usr/share/postgresql/16',
           '--auth-local=trust', '--auth-host=scram-sha-256', '--encoding=UTF8', '--locale=C.UTF-8')
    start_db()
    asyncio.run(setup_database())
    subprocess.run([str(PYTHON), '-m', 'datum_sync.migrate'], cwd=APP, env=environment(), check=True)
    subprocess.run([str(PYTHON), '-m', 'datum_sync.portal_seed'], cwd=APP, env=environment(), check=True)


def start_db():
    status = subprocess.run([str(PG/'pg_ctl'), '-D', str(RUNTIME/'pgdata'), 'status'],
                            env=environment(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if status.returncode:
        pg('pg_ctl', '-D', RUNTIME/'pgdata', '-l', RUNTIME/'postgres.log', '-o',
           f'-p 55435 -h 127.0.0.1 -k {RUNTIME / "socket"}', '-w', 'start')


async def setup_database():
    import asyncpg
    conn = await asyncpg.connect(host=str(RUNTIME/'socket'), port=55435, database='postgres')
    try:
        if not await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname='datum_portal'"):
            password = environment()['DATUM_PG_PASSWORD']
            await conn.execute("CREATE ROLE datum_portal LOGIN PASSWORD '" + password.replace("'", "''") + "'")
        for name in ('datum_portal', 'datum_portal_test'):
            if not await conn.fetchval('SELECT 1 FROM pg_database WHERE datname=$1', name):
                await conn.execute(f'CREATE DATABASE {name} OWNER datum_portal')
            db = await asyncpg.connect(host=str(RUNTIME/'socket'), port=55435, database=name)
            try:
                await db.execute('CREATE EXTENSION IF NOT EXISTS postgis; CREATE EXTENSION IF NOT EXISTS pgcrypto')
            finally:
                await db.close()
    finally:
        await conn.close()


def service_pid(file_name, module):
    file = RUNTIME/file_name
    if not file.exists():
        return None
    pid = int(file.read_text())
    try:
        command = Path(f'/proc/{pid}/cmdline').read_bytes()
        if module.encode() in command and str(PYTHON).encode() in command:
            return pid
    except FileNotFoundError:
        pass
    return None


def running_pid():
    return service_pid('portal.pid','datum_sync.portal:app')


def adapter_pid():
    return service_pid('legacy-mcp.pid','datum_sync.legacy_mcp:app')


def demo_adapter_pid():
    return service_pid('demo-mcp.pid','datum_sync.demo_mcp:app')


def worker_pid():
    return service_pid('worker.pid','app:app')


def start_adapter():
    if adapter_pid():
        return
    with (RUNTIME/'legacy-mcp.log').open('ab') as log:
        process = subprocess.Popen([str(PYTHON), '-m', 'uvicorn', 'datum_sync.legacy_mcp:app',
                                    '--host', '127.0.0.1', '--port', '8211',
                                    '--no-proxy-headers', '--no-access-log'],
                                   cwd=APP, env=environment(), stdout=log, stderr=log, start_new_session=True)
    (RUNTIME/'legacy-mcp.pid').write_text(str(process.pid))
    import urllib.request
    request=urllib.request.Request('http://127.0.0.1:8211/health',
        headers={'Authorization':'Bearer '+environment()['LEGACY_MCP_TOKEN']})
    for _ in range(50):
        try:
            with urllib.request.urlopen(request,timeout=1) as response:
                if response.status==200:
                    return
        except OSError:
            time.sleep(.2)
    raise RuntimeError(f'Legacy MCP adapter did not start; inspect {RUNTIME / "legacy-mcp.log"}')


def start_demo_adapter():
    if demo_adapter_pid():
        return
    with (RUNTIME/'demo-mcp.log').open('ab') as log:
        process = subprocess.Popen([str(PYTHON), '-m', 'uvicorn', 'datum_sync.demo_mcp:app',
                                    '--host', '127.0.0.1', '--port', '8212',
                                    '--no-proxy-headers', '--no-access-log'],
                                   cwd=APP, env=environment(), stdout=log, stderr=log, start_new_session=True)
    (RUNTIME/'demo-mcp.pid').write_text(str(process.pid))
    import urllib.request
    for _ in range(50):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8212/health',timeout=1) as response:
                if response.status==200:
                    return
        except OSError:
            time.sleep(.2)
    raise RuntimeError(f'Demo MCP adapter did not start; inspect {RUNTIME / "demo-mcp.log"}')


def start_worker():
    if worker_pid():
        return
    allowed = ('HOME', 'PATH', 'LANG', 'LC_ALL', 'TZ', 'SSL_CERT_FILE',
               'SSL_CERT_DIR', 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY',
               'DATUM_SYNC_URL', 'WORKER_URL', 'DATUM_WORKER_MODEL', 'CODEX_BIN')
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env['PYTHONPATH'] = str(WORKER)
    with (RUNTIME/'worker.log').open('ab') as log:
        process = subprocess.Popen([str(PYTHON), '-m', 'uvicorn', 'app:app',
                                    '--host', '127.0.0.1', '--port', '8220',
                                    '--no-proxy-headers', '--no-access-log'],
                                   cwd=WORKER, env=env, stdout=log, stderr=log,
                                   start_new_session=True)
    (RUNTIME/'worker.pid').write_text(str(process.pid))
    import urllib.request
    for _ in range(50):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8220/health', timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(.2)
    raise RuntimeError(f'Datum worker did not start; inspect {RUNTIME / "worker.log"}')


def start():
    if not (RUNTIME/'config.json').exists():
        init()
    ensure_config()
    start_db()
    subprocess.run([str(PYTHON), '-m', 'datum_sync.migrate'], cwd=APP, env=environment(), check=True)
    subprocess.run([str(PYTHON), '-m', 'datum_sync.portal_seed'], cwd=APP, env=environment(), check=True)
    start_adapter()
    start_demo_adapter()
    if running_pid():
        start_worker()
        print('Portal already running at http://127.0.0.1:8210')
        return
    with (RUNTIME/'portal.log').open('ab') as log:
        process = subprocess.Popen([str(PYTHON), '-m', 'uvicorn', 'datum_sync.portal:app',
                                    '--host', '127.0.0.1', '--port', '8210', '--no-proxy-headers', '--no-access-log'],
                                   cwd=APP, env=environment(), stdout=log, stderr=log, start_new_session=True)
    (RUNTIME/'portal.pid').write_text(str(process.pid))
    import urllib.request
    for _ in range(50):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8210/health', timeout=1) as response:
                if response.status == 200:
                    start_worker()
                    print('Portal ready: http://127.0.0.1:8210')
                    print('Federated worker ready: http://127.0.0.1:8220')
                    print(f'Operator credentials: {RUNTIME / "operator.txt"}')
                    return
        except OSError:
            time.sleep(.2)
    raise RuntimeError(f'Portal did not start; inspect {RUNTIME / "portal.log"}')


def stop():
    for pid_getter in (worker_pid, running_pid, adapter_pid, demo_adapter_pid):
        pid = pid_getter()
        if pid:
            os.kill(pid, signal.SIGTERM)
            for _ in range(50):
                if not pid_getter():
                    break
                time.sleep(.1)
    print('Portal, worker and MCP adapters stopped; private database left running.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['init','start','stop','restart','status','test'])
    args = parser.parse_args()
    if args.action == 'init': init()
    elif args.action == 'start': start()
    elif args.action == 'stop': stop()
    elif args.action == 'restart': stop(); start()
    elif args.action == 'status':
        print('portal: '+('running' if running_pid() else 'stopped'))
        print('legacy MCP adapter: '+('running' if adapter_pid() else 'stopped'))
        print('demo MCP providers: '+('running' if demo_adapter_pid() else 'stopped'))
        print('federated worker: '+('running' if worker_pid() else 'stopped'))
    elif args.action == 'test':
        ensure_config()
        start_db()
        env = environment()
        env['DATABASE_URL'] = env['DATABASE_URL'].rsplit('/',1)[0] + '/datum_portal_test'
        env['PORTAL_TEST_DATABASE'] = '1'
        subprocess.run([str(PYTHON), '-m', 'datum_sync.migrate'], cwd=APP, env=env, check=True)
        result = subprocess.run([str(PYTHON), '-m', 'pytest', '-q', 'tests/test_portal.py', 'tests/test_mcp_federation.py', 'tests/test_mcp_federation_credentials.py', 'tests/test_mcp_observability.py', 'tests/test_credential_access.py', 'tests/test_mcp_server_registry.py', 'tests/test_governed_resources.py', 'tests/test_security_repairs.py', 'tests/test_persona_profiles.py'], cwd=APP, env=env)
        if result.returncode == 0:
            worker_env = environment()
            worker_env['PYTHONPATH'] = str(WORKER)
            result = subprocess.run([str(PYTHON), '-m', 'pytest', '-q', 'tests'], cwd=WORKER, env=worker_env)
        sys.exit(result.returncode)


if __name__ == '__main__':
    main()
