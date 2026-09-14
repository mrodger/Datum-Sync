"""Compare both local schemas with a fresh migration build, without elevating the app role."""
import asyncio
import os
import sys
import uuid
import importlib.util

import manage

os.environ.update(manage.environment())
sys.path.insert(0, str(manage.APP))
import asyncpg


async def main():
    spec = importlib.util.spec_from_file_location('schema_check', manage.APP/'tests/test_schema_drift.py')
    checks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checks)
    name = 'datum_schema_' + uuid.uuid4().hex[:12]
    admin = await asyncpg.connect(host=str(manage.RUNTIME/'socket'), port=55435, database='postgres')
    created = False
    try:
        await admin.execute(f'CREATE DATABASE {name} OWNER datum_portal')
        created = True
        extensions = await asyncpg.connect(host=str(manage.RUNTIME/'socket'), port=55435, database=name)
        try:
            await extensions.execute('CREATE EXTENSION postgis; CREATE EXTENSION pgcrypto')
        finally:
            await extensions.close()
        base = manage.environment()['DATABASE_URL'].rsplit('/', 1)[0]
        await checks._build_from_migrations(base+'/'+name)
        scratch = await asyncpg.connect(base+'/'+name)
        try:
            expected = await checks._snapshot(scratch)
        finally:
            await scratch.close()
        for database in ('datum_portal', 'datum_portal_test'):
            live = await asyncpg.connect(base+'/'+database)
            try:
                actual = await checks._snapshot(live)
            finally:
                await live.close()
            differences = []
            for kind in expected:
                differences.extend(checks._describe(kind, actual[kind]-expected[kind], expected[kind]-actual[kind]))
            assert not differences, '\n'.join(differences)
            print('PASS: '+database+' matches a fresh migration build.')
    finally:
        if created:
            await admin.execute(f'DROP DATABASE {name}')
        await admin.close()


if __name__ == '__main__':
    asyncio.run(main())
