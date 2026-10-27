"""CivicServe: zero-dependency municipal e-governance demo.

Run: python app.py    Tests: python -m unittest discover -s tests -v
Uses only Python's standard library. This is an educational prototype,
not a substitute for a production identity, authorization or records system.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv('CIVICSERVE_DATA_DIR', str(ROOT / 'data')))
DB_PATH = Path(os.getenv('CIVICSERVE_DB', str(DATA_DIR / 'civicserve.sqlite3')))
HOST = os.getenv('CIVICSERVE_HOST', '127.0.0.1')
PORT = int(os.getenv('CIVICSERVE_PORT', '8000'))
DEMO_SEED = os.getenv('CIVICSERVE_DEMO_SEED', '1') == '1'
SECURE_COOKIES = os.getenv('CIVICSERVE_SECURE_COOKIES', '0') == '1'
SESSION_AGE = 7 * 24 * 60 * 60
STATIC_DIR = ROOT / 'static'
MAX_JSON_BYTES = 64 * 1024

SERVICE_CATALOG = [
 ('birth-certificate','Birth certificate','Certificates','Request an official birth certificate or a certified duplicate.','baby',7,'Hospital record, parent identification'),
 ('residence-certificate','Residence certificate','Certificates','Apply for a proof of municipal residence.','home',5,'Address proof, government identification'),
 ('trade-license','Trade license','Business','Register or renew permission for a local business.','store',14,'Business identity, premises details'),
 ('water-connection','Water connection','Utilities','Apply for a new municipal water supply connection.','droplet',10,'Property proof, site address'),
 ('property-tax','Property tax correction','Property','Request a correction to property tax assessment records.','file',12,'Property ID, prior tax assessment'),
 ('streetlight','Streetlight repair','Civic issues','Report a malfunctioning or broken public streetlight.','sun',3,'Location and pole identification if available'),
 ('waste-collection','Waste collection','Civic issues','Report missed waste pickup or request sanitation services.','recycle',2,'Pickup address and issue details'),
 ('road-repair','Road maintenance','Civic issues','Report potholes or damaged municipal roads.','route',7,'Road location and description'),
]

class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


def utc_now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def db_connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA busy_timeout = 10000')
    conn.execute('PRAGMA journal_mode = WAL')
    return conn


def as_dict(row):
    return dict(row) if row is not None else None


def hash_password(password, salt=None):
    salt = secrets.token_bytes(16) if salt is None else salt
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf8'), salt, 260_000)
    return f'pbkdf2_sha256$260000${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}'


def check_password(password, encoded):
    try:
        scheme, rounds, salt64, digest64 = encoded.split('$')
        if scheme != 'pbkdf2_sha256':
            return False
        candidate = hashlib.pbkdf2_hmac('sha256', password.encode(), base64.urlsafe_b64decode(salt64), int(rounds))
        return hmac.compare_digest(candidate, base64.urlsafe_b64decode(digest64))
    except (TypeError, ValueError, OverflowError):
        return False


def row_one(db, sql, params=()):
    return as_dict(db.execute(sql, params).fetchone())


def rows(db, sql, params=()):
    return [dict(row) for row in db.execute(sql, params).fetchall()]


def validate_text(value, label, min_len=1, max_len=2000):
    if not isinstance(value, str):
        raise ApiError(f'{label} is required.')
    s = value.strip()
    if len(s) < min_len or len(s) > max_len:
        raise ApiError(f'{label} must contain {min_len}–{max_len} characters.')
    return s


def record_audit(db, actor, action, application_id=None, details='', ip=''):
    db.execute('INSERT INTO audit_logs(actor_id,application_id,action,details,ip_address) VALUES (?,?,?,?,?)',
               (actor, application_id, action, str(details)[:1200], ip[:80]))


def event(db, case_id, actor, event_type, note='', before=None, after=None):
    db.execute('INSERT INTO case_events(application_id,actor_id,event_type,note,from_status,to_status) VALUES (?,?,?,?,?,?)',
               (case_id, actor, event_type, note[:1200], before, after))


def notify(db, user_id, case_id, message):
    db.execute('INSERT INTO notifications(user_id,application_id,message) VALUES (?,?,?)', (user_id,case_id,message[:350]))


def admin_ids(db):
    return [r['id'] for r in rows(db, "SELECT id FROM users WHERE role='admin'")]


def initialize_database(seed=DEMO_SEED):
    db = db_connect()
    try:
        db.executescript((ROOT / 'schema.sql').read_text(encoding='utf8'))
        with db:
            if db.execute('SELECT COUNT(*) FROM services').fetchone()[0] == 0:
                db.executemany('INSERT INTO services(slug,title,category,description,icon,eta_days,requirements) VALUES (?,?,?,?,?,?,?)',SERVICE_CATALOG)
            if seed and db.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0:
                insert_demo_data(db)
    finally:
        db.close()


def insert_demo_data(db):
    demo_users = [
        ('Aarav Mehta','citizen@civicserve.local','citizen'),
        ('Priya Sharma','officer@civicserve.local','officer'),
        ('Rohan Das','officer2@civicserve.local','officer'),
        ('Ananya Rao','admin@civicserve.local','admin'),
        ('Meera Patel','meera@example.local','citizen'),
    ]
    for name,email,role in demo_users:
        db.execute('INSERT INTO users(name,email,password_hash,role) VALUES (?,?,?,?)',
                   (name,email,hash_password('Demo@12345'),role))
    users = {r['email']:r['id'] for r in rows(db,'SELECT id,email FROM users')}
    svcs = {r['slug']:r['id'] for r in rows(db,'SELECT id,slug FROM services')}
    samples = [
        ('citizen@civicserve.local','birth-certificate','Certified copy for dependent','Requesting a certified copy of my daughter’s birth registration.','12 Lakeview Avenue, Ward 04','approved','officer@civicserve.local'),
        ('citizen@civicserve.local','property-tax','Property record correction','The spelling of my surname is incorrect in the annual assessment.','88 Park Road, Ward 06','in_review','officer@civicserve.local'),
        ('citizen@civicserve.local','water-connection','New water connection','Requesting a domestic water connection at a newly occupied property.','19 Rose Street, Ward 02','needs_info','officer2@civicserve.local'),
        ('citizen@civicserve.local','streetlight','Streetlight not working','Streetlamp near the community park has stopped working.','Community Park, Ward 04','submitted',None),
        ('citizen@civicserve.local','trade-license','Small business permit','Applying for a local retail shop trade license.','44 Market Lane, Ward 01','rejected','officer2@civicserve.local'),
        ('meera@example.local','waste-collection','Missed neighborhood pickup','Garbage has not been collected for three days.','5 East Lane, Ward 09','assigned','officer@civicserve.local'),
        ('meera@example.local','residence-certificate','Residency verification','Certificate required for a university application.','14 Garden Street, Ward 09','approved','officer2@civicserve.local'),
        ('meera@example.local','road-repair','Dangerous pothole','Road surface has deep potholes near the school gate.','School Road, Ward 08','in_review','officer@civicserve.local'),
        ('meera@example.local','property-tax','Tax receipt correction','Paid assessment has not been updated in the property record.','21 Palm Drive, Ward 05','submitted',None),
        ('meera@example.local','water-connection','Existing pipe leakage','Water service connection appears to be leaking.','37 North Avenue, Ward 03','assigned','officer2@civicserve.local'),
    ]
    for i,(email,slug,subject,description,address,status,off_email) in enumerate(samples,1):
        owner = users[email]; officer = users.get(off_email)
        cur = db.execute('INSERT INTO applications(reference,citizen_id,service_id,officer_id,status,subject,description,address,closed_at) VALUES (?,?,?,?,?,?,?,?,?)',
                         (f'CIV-{datetime.now(timezone.utc).year}-{i:05}',owner,svcs[slug],officer,status,subject,description,address,utc_now() if status in ('approved','rejected') else None))
        case_id = cur.lastrowid
        event(db,case_id,owner,'submitted','Application submitted',None,'submitted')
        if officer:
            event(db,case_id,users['admin@civicserve.local'],'assigned',f'Assigned to {off_email}','submitted','assigned')
        if status not in ('assigned','submitted'):
            event(db,case_id,officer,'review_started','Case taken into review','assigned','in_review')
        if status == 'needs_info':
            event(db,case_id,officer,'needs_info','Please provide the latest property ownership record number in your response.','in_review','needs_info')
            notify(db,owner,case_id,f'{subject}: additional information requested')
        elif status in ('approved','rejected'):
            event(db,case_id,officer,status,'Decision recorded after document review.','in_review',status)
            notify(db,owner,case_id,f'{subject}: application {status}')
        record_audit(db,owner,'application.created',case_id,'Demo seed data')
    notify(db,users['admin@civicserve.local'],None,'Welcome to CivicServe. Review the assignment queue.')


def get_case(db, case_id):
    return row_one(db, '''SELECT a.*,s.title AS service_title,s.slug AS service_slug,s.category AS service_category,
            s.icon AS service_icon,s.eta_days AS eta_days,c.name AS citizen_name,c.email AS citizen_email,
            o.name AS officer_name,o.email AS officer_email
            FROM applications a JOIN services s ON s.id=a.service_id
            JOIN users c ON c.id=a.citizen_id LEFT JOIN users o ON o.id=a.officer_id WHERE a.id=?''',(case_id,))


def viewable(case,user):
    return (user['role']=='admin' or (user['role']=='citizen' and case['citizen_id']==user['id'])
            or (user['role']=='officer' and case['officer_id']==user['id']))


class Handler(BaseHTTPRequestHandler):
    server_version = 'CivicServe/1.0'
    sys_version = ''

    def log_message(self, fmt, *args):
        if os.getenv('CIVICSERVE_QUIET') != '1':
            super().log_message(fmt,*args)

    def send_json(self, payload, status=200, cookies=()):
        body = json.dumps(payload, ensure_ascii=False, separators=(',',':')).encode('utf8')
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.security_headers()
        for cookie in cookies:
            self.send_header('Set-Cookie',cookie)
        self.end_headers()
        self.wfile.write(body)

    def security_headers(self):
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','strict-origin-when-cross-origin')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")

    def cookie(self,name):
        cookie=SimpleCookie()
        try: cookie.load(self.headers.get('Cookie',''))
        except Exception: return None
        return cookie[name].value if name in cookie else None

    def cookie_header(self,name,value,max_age):
        suffix = '; Secure' if SECURE_COOKIES else ''
        return f'{name}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict{suffix}'

    def current_user(self,db):
        raw=self.cookie('cs_session')
        if not raw: return None
        token_hash=hashlib.sha256(raw.encode()).hexdigest()
        return row_one(db,'''SELECT u.id,u.name,u.email,u.role FROM sessions s JOIN users u ON u.id=s.user_id
                             WHERE s.token_hash=? AND s.expires_at>?''',(token_hash,int(time.time())))

    def require_user(self,db,role=None):
        user=self.current_user(db)
        if not user: raise ApiError('Please sign in to continue.',401)
        if role and user['role'] not in (role if isinstance(role,tuple) else (role,)):
            raise ApiError('You do not have permission to do that.',403)
        return user

    def check_csrf(self):
        cookie=self.cookie('cs_csrf')
        header=self.headers.get('X-CSRF-Token','')
        if not cookie or not header or not hmac.compare_digest(cookie,header):
            raise ApiError('Your security token expired. Refresh the page and try again.',403)
        origin=self.headers.get('Origin')
        if origin:
            expected=f'{"https" if SECURE_COOKIES else "http"}://{self.headers.get("Host","")}'
            if origin != expected:
                raise ApiError('Cross-site requests are not permitted.',403)

    def body_json(self):
        if self.headers.get('Content-Type','').split(';')[0] != 'application/json':
            raise ApiError('Content-Type must be application/json.',415)
        try: length=int(self.headers.get('Content-Length','0'))
        except ValueError: raise ApiError('Invalid request size.')
        if length<0 or length>MAX_JSON_BYTES: raise ApiError('Request body too large.',413)
        try: data=json.loads(self.rfile.read(length))
        except (ValueError,UnicodeDecodeError): raise ApiError('Invalid JSON payload.')
        if not isinstance(data,dict): raise ApiError('Expected a JSON object.')
        return data

    def do_GET(self): self.dispatch('GET')
    def do_POST(self): self.dispatch('POST')

    def dispatch(self,method):
        parsed=urlparse(self.path); path=parsed.path
        if not path.startswith('/api/'):
            if method!='GET': return self.send_json({'error':'Method not allowed.'},405)
            return self.serve_frontend(path)
        db=db_connect()
        try:
            if method=='POST': self.check_csrf()
            if method=='GET':
                result=self.handle_get(db,path,parse_qs(parsed.query))
            else:
                # One database transaction per mutation for audit consistency.
                db.execute('BEGIN IMMEDIATE')
                try:
                    result=self.handle_post(db,path,self.body_json())
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
            if result is not None:
                self.send_json(result)
        except ApiError as e:
            self.send_json({'error':e.message},e.status)
        except sqlite3.IntegrityError:
            self.send_json({'error':'The requested change conflicts with existing data.'},409)
        except (BrokenPipeError,ConnectionResetError):
            pass
        except Exception:
            import traceback
            traceback.print_exc()
            try:self.send_json({'error':'An unexpected server error occurred.'},500)
            except (BrokenPipeError,ConnectionResetError): pass
        finally:
            db.close()

    def serve_frontend(self,path):
        files={'/':'index.html','/index.html':'index.html','/style.css':'style.css','/app.js':'app.js','/favicon.svg':'favicon.svg'}
        name=files.get(path)
        if not name:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data=(STATIC_DIR/name).read_bytes()
        mime={'html':'text/html; charset=utf-8','css':'text/css; charset=utf-8','js':'text/javascript; charset=utf-8','svg':'image/svg+xml'}[name.rsplit('.',1)[-1]]
        self.send_response(200)
        self.send_header('Content-Type',mime)
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-cache')
        self.security_headers()
        self.end_headers()
        self.wfile.write(data)

    def handle_get(self,db,path,query):
        if path=='/api/health': return {'ok':True,'service':'CivicServe','timestamp':utc_now()}
        if path=='/api/bootstrap':
            user=self.current_user(db)
            csrf=self.cookie('cs_csrf') or secrets.token_urlsafe(32)
            unread= db.execute('SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0',(user['id'],)).fetchone()[0] if user else 0
            return self.send_json({'user':user,'csrf':csrf,'unread':unread},cookies=[self.cookie_header('cs_csrf',csrf,SESSION_AGE)])
        if path=='/api/services':
            self.require_user(db)
            return {'services':rows(db,'SELECT * FROM services WHERE active=1 ORDER BY id')}
        if path=='/api/dashboard':
            user=self.require_user(db)
            role=user['role']
            clause,params=self.visibility_clause(user)
            stats=row_one(db,f'''SELECT COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN status IN ('submitted','assigned') THEN 1 ELSE 0 END),0) AS pending,
                COALESCE(SUM(CASE WHEN status IN ('in_review','needs_info') THEN 1 ELSE 0 END),0) AS active,
                COALESCE(SUM(CASE WHEN status='approved' THEN 1 ELSE 0 END),0) AS approved,
                COALESCE(SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END),0) AS rejected
                FROM applications a WHERE {clause}''',params)
            recent=rows(db,f'''SELECT a.id,a.reference,a.subject,a.status,a.updated_at,
                       s.title AS service_title,s.icon AS service_icon,c.name AS citizen_name
                       FROM applications a JOIN services s ON s.id=a.service_id
                       JOIN users c ON c.id=a.citizen_id WHERE {clause}
                       ORDER BY a.id DESC LIMIT 6''',params)
            categories=rows(db,f'''SELECT s.category AS label,COUNT(*) AS value FROM applications a
                       JOIN services s ON s.id=a.service_id WHERE {clause} GROUP BY s.category ORDER BY value DESC''',params)
            pipeline=rows(db,f'''SELECT status,COUNT(*) AS count FROM applications a WHERE {clause} GROUP BY status''',params)
            officer_load=rows(db,'''SELECT u.id,u.name,COUNT(a.id) AS cases FROM users u LEFT JOIN applications a
                ON a.officer_id=u.id AND a.status NOT IN ('approved','rejected') WHERE u.role='officer'
                GROUP BY u.id ORDER BY cases DESC''') if role=='admin' else []
            return {'stats':stats,'recent':recent,'categories':categories,'pipeline':pipeline,'officer_load':officer_load}
        if path=='/api/applications':
            user=self.require_user(db)
            clause,params=self.visibility_clause(user)
            st=query.get('status',['all'])[0]
            if st!='all':
                if st not in ('submitted','assigned','in_review','needs_info','approved','rejected'):
                    raise ApiError('Invalid status filter.')
                clause+=' AND a.status=?';params.append(st)
            q=query.get('q',[''])[0].strip()[:100]
            if q:
                clause+=' AND (a.reference LIKE ? OR a.subject LIKE ? OR s.title LIKE ?)'
                params.extend(['%'+q+'%']*3)
            items=rows(db,f'''SELECT a.id,a.reference,a.subject,a.status,a.created_at,a.updated_at,
                s.title AS service_title,s.icon AS service_icon,c.name AS citizen_name,
                o.name AS officer_name FROM applications a JOIN services s ON s.id=a.service_id
                JOIN users c ON c.id=a.citizen_id LEFT JOIN users o ON o.id=a.officer_id
                WHERE {clause} ORDER BY a.updated_at DESC,a.id DESC LIMIT 200''',params)
            return {'applications':items}
        match=re.fullmatch(r'/api/applications/(\d+)',path)
        if match:
            user=self.require_user(db)
            case=get_case(db,int(match[1]))
            if not case: raise ApiError('Application not found.',404)
            if not viewable(case,user): raise ApiError('You cannot access this application.',403)
            timeline=rows(db,'''SELECT e.*,u.name AS actor_name,u.role AS actor_role FROM case_events e
                               LEFT JOIN users u ON u.id=e.actor_id WHERE e.application_id=? ORDER BY e.id DESC''',(case['id'],))
            return {'application':case,'timeline':timeline}
        if path=='/api/officers':
            self.require_user(db,'admin')
            return {'officers':rows(db,'''SELECT u.id,u.name,u.email,
                COUNT(a.id) AS active_cases FROM users u LEFT JOIN applications a
                ON a.officer_id=u.id AND a.status NOT IN ('approved','rejected')
                WHERE u.role='officer' GROUP BY u.id ORDER BY u.name''')}
        if path=='/api/notifications':
            user=self.require_user(db)
            return {'notifications':rows(db,'''SELECT n.* ,a.reference FROM notifications n
                LEFT JOIN applications a ON a.id=n.application_id WHERE n.user_id=?
                ORDER BY n.id DESC LIMIT 100''',(user['id'],))}
        if path=='/api/audit':
            self.require_user(db,'admin')
            return {'logs':rows(db,'''SELECT l.*,u.name AS actor_name,u.role AS actor_role,
                a.reference FROM audit_logs l LEFT JOIN users u ON u.id=l.actor_id
                LEFT JOIN applications a ON a.id=l.application_id ORDER BY l.id DESC LIMIT 250''')}
        raise ApiError('API route not found.',404)

    def visibility_clause(self,user):
        if user['role']=='admin': return '1=1',[]
        if user['role']=='officer': return 'a.officer_id=?',[user['id']]
        return 'a.citizen_id=?',[user['id']]

    def handle_post(self,db,path,data):
        if path=='/api/auth/register':
            name=validate_text(data.get('name'),'Full name',2,90)
            email=validate_text(data.get('email'),'Email',5,254).lower()
            if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email): raise ApiError('Enter a valid email address.')
            password=data.get('password','')
            if not isinstance(password,str) or len(password)<10 or len(password)>128:
                raise ApiError('Password must contain 10–128 characters.')
            if not re.search(r'[A-Za-z]',password) or not re.search(r'\d',password):
                raise ApiError('Password must include a letter and a number.')
            if row_one(db,'SELECT id FROM users WHERE email=?',(email,)):
                raise ApiError('An account with this email already exists.',409)
            cur=db.execute("INSERT INTO users(name,email,password_hash,role) VALUES (?,?,?,'citizen')",
                           (name,email,hash_password(password)))
            user_id=cur.lastrowid
            record_audit(db,user_id,'user.registered',ip=self.client_address[0])
            return self.start_session(db,user_id)
        if path=='/api/auth/login':
            email=str(data.get('email','')).strip().lower()
            password=data.get('password','')
            user=row_one(db,'SELECT * FROM users WHERE email=?',(email,))
            if not isinstance(password,str) or not user or not check_password(password,user['password_hash']):
                raise ApiError('Invalid email or password.',401)
            record_audit(db,user['id'],'auth.login',ip=self.client_address[0])
            return self.start_session(db,user['id'])
        if path=='/api/auth/logout':
            raw=self.cookie('cs_session')
            user=self.current_user(db)
            if raw:
                db.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(raw.encode()).hexdigest(),))
            if user:record_audit(db,user['id'],'auth.logout',ip=self.client_address[0])
            db.commit()
            return self.send_json({'ok':True},cookies=[self.cookie_header('cs_session','',0)])
        if path=='/api/officers':
            user=self.require_user(db,'admin')
            name=validate_text(data.get('name'),'Officer name',2,90)
            email=validate_text(data.get('email'),'Officer email',5,254).lower()
            if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email):
                raise ApiError('Enter a valid email address.')
            password=data.get('password','')
            if not isinstance(password,str) or len(password)<10 or len(password)>128 or not re.search(r'[A-Za-z]',password) or not re.search(r'\d',password):
                raise ApiError('Password must be 10–128 characters and contain letters and numbers.')
            if row_one(db,'SELECT id FROM users WHERE email=?',(email,)):
                raise ApiError('An account with this email already exists.',409)
            cur=db.execute("INSERT INTO users(name,email,password_hash,role) VALUES (?,?,?,'officer')",(name,email,hash_password(password)))
            record_audit(db,user['id'],'officer.created',None,email,self.client_address[0])
            notify(db,cur.lastrowid,None,'Your CivicServe officer account has been created.')
            return {'message':'Officer account created successfully.'}
        if path=='/api/applications':
            user=self.require_user(db,'citizen')
            service_id=data.get('service_id')
            if type(service_id) is not int: raise ApiError('Select a service.')
            service=row_one(db,'SELECT * FROM services WHERE id=? AND active=1',(service_id,))
            if not service: raise ApiError('Service not available.',404)
            subject=validate_text(data.get('subject'),'Subject',5,140)
            description=validate_text(data.get('description'),'Description',15,2000)
            address=validate_text(data.get('address'),'Service address',8,250)
            cur=db.execute('''INSERT INTO applications(reference,citizen_id,service_id,status,subject,description,address)
                    VALUES (?,?,?,?,?,?,?)''',(f'TMP-{secrets.token_hex(12)}',user['id'],service_id,'submitted',subject,description,address))
            case_id=cur.lastrowid
            ref=f'CIV-{datetime.now(timezone.utc).year}-{case_id:05d}'
            db.execute('UPDATE applications SET reference=? WHERE id=?',(ref,case_id))
            event(db,case_id,user['id'],'submitted','Application submitted',None,'submitted')
            record_audit(db,user['id'],'application.created',case_id,service['title'],self.client_address[0])
            for administrator in admin_ids(db):
                notify(db,administrator,case_id,f'New application {ref}: {service["title"]}')
            return {'id':case_id,'reference':ref,'message':'Application submitted successfully.'}
        match=re.fullmatch(r'/api/applications/(\d+)/(assign|transition|reply)',path)
        if match:
            case_id=int(match[1]);action=match[2]
            user=self.require_user(db)
            case=get_case(db,case_id)
            if not case: raise ApiError('Application not found.',404)
            if action=='assign':
                if user['role']!='admin': raise ApiError('Only administrators can assign cases.',403)
                if case['status'] in ('approved','rejected'):
                    raise ApiError('Closed cases cannot be reassigned.',409)
                officer_id=data.get('officer_id')
                if type(officer_id) is not int:raise ApiError('Select an officer.')
                officer=row_one(db,"SELECT id,name FROM users WHERE id=? AND role='officer'",(officer_id,))
                if not officer: raise ApiError('Select a valid officer.',400)
                before=case['status']
                after='assigned' if before in ('submitted','assigned') else before
                db.execute('''UPDATE applications SET officer_id=?,status=?,updated_at=? WHERE id=?''',
                           (officer_id,after,utc_now(),case_id))
                note=f'Assigned to {officer["name"]}'
                event(db,case_id,user['id'],'assigned',note,before,after)
                record_audit(db,user['id'],'application.assigned',case_id,note,self.client_address[0])
                notify(db,officer_id,case_id,f'{case["reference"]}: a case has been assigned to you')
                notify(db,case['citizen_id'],case_id,f'{case["reference"]}: assigned to {officer["name"]}')
                return {'message':'Officer assigned successfully.'}
            if action=='transition':
                if user['role']!='officer' or case['officer_id']!=user['id']:
                    raise ApiError('Only the assigned officer can review this case.',403)
                choice=data.get('action')
                transitions={'assigned':{'start':'in_review'},
                             'in_review':{'needs_info':'needs_info','approve':'approved','reject':'rejected'}}
                new_status=transitions.get(case['status'],{}).get(choice)
                if not new_status:raise ApiError('That action is not allowed in the current case status.',409)
                note=str(data.get('note','')).strip()
                if choice in ('needs_info','reject'):
                    note=validate_text(note,'Decision note',8,1200)
                elif len(note)>1200:raise ApiError('Note is too long.')
                if choice=='start' and not note:note='Case review started'
                closed=utc_now() if new_status in ('approved','rejected') else None
                db.execute('''UPDATE applications SET status=?,updated_at=?,closed_at=? WHERE id=?''',
                           (new_status,utc_now(),closed,case_id))
                event(db,case_id,user['id'],choice,note,case['status'],new_status)
                record_audit(db,user['id'],'application.'+choice,case_id,note,self.client_address[0])
                notify(db,case['citizen_id'],case_id,
                       f'{case["reference"]}: {"additional information requested" if new_status=="needs_info" else new_status.replace("_"," ")}')
                return {'message':'Case updated successfully.','status':new_status}
            if action=='reply':
                if user['role']!='citizen' or case['citizen_id']!=user['id']:
                    raise ApiError('Only the applicant can submit the requested information.',403)
                if case['status']!='needs_info':
                    raise ApiError('This case is not awaiting additional information.',409)
                note=validate_text(data.get('message'),'Response',10,1200)
                new_status='in_review' if case['officer_id'] else 'submitted'
                db.execute('UPDATE applications SET status=?,updated_at=? WHERE id=?',(new_status,utc_now(),case_id))
                event(db,case_id,user['id'],'citizen_replied',note,'needs_info',new_status)
                record_audit(db,user['id'],'application.replied',case_id,note,self.client_address[0])
                if case['officer_id']:
                    notify(db,case['officer_id'],case_id,f'{case["reference"]}: applicant responded to your request')
                return {'message':'Your response has been sent.','status':new_status}
        if path=='/api/notifications/read-all':
            user=self.require_user(db)
            db.execute('UPDATE notifications SET is_read=1 WHERE user_id=?',(user['id'],))
            return {'ok':True}
        match=re.fullmatch(r'/api/notifications/(\d+)/read',path)
        if match:
            user=self.require_user(db)
            cur=db.execute('UPDATE notifications SET is_read=1 WHERE id=? AND user_id=?',(int(match[1]),user['id']))
            if not cur.rowcount: raise ApiError('Notification not found.',404)
            return {'ok':True}
        raise ApiError('API route not found.',404)

    def start_session(self,db,user_id):
        raw=secrets.token_urlsafe(40)
        digest=hashlib.sha256(raw.encode()).hexdigest()
        db.execute('INSERT INTO sessions(token_hash,user_id,expires_at) VALUES (?,?,?)',
                   (digest,user_id,int(time.time())+SESSION_AGE))
        user=row_one(db,'SELECT id,name,email,role FROM users WHERE id=?',(user_id,))
        db.commit()  # session must be visible before the response reaches the browser
        return self.send_json({'user':user},cookies=[self.cookie_header('cs_session',raw,SESSION_AGE)])


def run_server(host=HOST,port=PORT,seed=DEMO_SEED):
    initialize_database(seed=seed)
    server=ThreadingHTTPServer((host,port),Handler)
    print(f'CivicServe running at http://{host}:{server.server_port}')
    print('For demo-only credentials, see README.md')
    try:server.serve_forever()
    except KeyboardInterrupt:print('\nStopping CivicServe.')
    finally:server.server_close()


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description='CivicServe development portal')
    p.add_argument('--host',default=HOST)
    p.add_argument('--port',type=int,default=PORT)
    p.add_argument('--no-demo',action='store_true',help='do not seed demo accounts')
    args=p.parse_args()
    run_server(args.host,args.port,not args.no_demo)
