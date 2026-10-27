"""Bootstrap an administrator when demo seeding is disabled.

CIVICSERVE_DEMO_SEED=0 python manage.py create-admin
"""
import getpass
import re
import sqlite3
import sys

import app


def main():
    if len(sys.argv) != 2 or sys.argv[1] != 'create-admin':
        print('Usage: python manage.py create-admin')
        sys.exit(2)
    app.initialize_database(seed=False)
    name=input('Administrator full name: ').strip()
    email=input('Administrator email: ').strip().lower()
    password=getpass.getpass('Administrator password: ')
    if not (2 <= len(name) <= 90 and re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email)):
        raise SystemExit('Invalid administrator name or email.')
    if len(password)<10 or len(password)>128 or not re.search('[A-Za-z]',password) or not re.search(r'\d',password):
        raise SystemExit('Use 10–128 characters, including letters and numbers.')
    db=app.db_connect()
    try:
        with db:
            db.execute("INSERT INTO users(name,email,password_hash,role) VALUES (?,?,?,'admin')",
                       (name,email,app.hash_password(password)))
    except sqlite3.IntegrityError:
        raise SystemExit('An account with this email already exists.')
    finally:
        db.close()
    print('Administrator created. Sign in to create officer accounts.')

if __name__=='__main__':
    main()
