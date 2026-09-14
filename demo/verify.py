"""Run database checks exclusively against this checkout's disposable test database."""
import argparse
import subprocess
import sys
import manage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['suite', 'guards'])
    args = parser.parse_args()
    manage.start_db()
    env = manage.environment()
    env['DATABASE_URL'] = env['DATABASE_URL'].rsplit('/', 1)[0] + '/datum_portal_test'
    env['PORTAL_TEST_DATABASE'] = '1'
    subprocess.run([str(manage.PYTHON), '-m', 'datum_sync.migrate'], cwd=manage.APP, env=env, check=True)
    command = (['-m', 'pytest', '-q'] if args.action == 'suite'
               else ['tests/break_the_guard.py', 'PORTAL', 'AUTH', 'PROXY', 'FED', 'JOB', 'MCPFLOW', 'CRED', 'SERVER', 'MCP-020'])
    return subprocess.run([str(manage.PYTHON), *command], cwd=manage.APP, env=env).returncode


if __name__ == '__main__':
    sys.exit(main())
