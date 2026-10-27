"""End-to-end HTTP/API tests (uses only Python standard library)."""
import http.cookiejar
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import app


class Client:
    def __init__(self,base):
        self.base=base
        self.cookies=http.cookiejar.CookieJar()
        self.opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        self.csrf=self.get('/bootstrap')['csrf']

    def request(self,path,method='GET',body=None,csrf=True):
        headers={}
        data=None
        if method=='POST':
            headers['Content-Type']='application/json'
            if csrf:headers['X-CSRF-Token']=self.csrf
            data=json.dumps(body or {}).encode()
        req=urllib.request.Request(self.base+'/api'+path,data=data,method=method,headers=headers)
        try:
            response=self.opener.open(req,timeout=10)
        except urllib.error.HTTPError as e:
            return e.code,json.loads(e.read().decode())
        with response:
            return response.status,json.loads(response.read().decode())

    def get(self,path):
        status,body=self.request(path)
        if status!=200:raise AssertionError((status,body))
        return body

    def post(self,path,payload=None):
        return self.request(path,method='POST',body=payload)

    def login(self,email,password='Demo@12345'):
        code,data=self.post('/auth/login',{'email':email,'password':password})
        if code!=200:raise AssertionError((code,data))
        return data['user']


class CivicServeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.saved_path=app.DB_PATH
        app.DB_PATH=Path(self.temp.name)/'test.sqlite3'
        app.initialize_database(seed=True)
        app.os.environ['CIVICSERVE_QUIET']='1'
        self.server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        base=f'http://127.0.0.1:{self.server.server_port}'
        self.public=Client(base)
        self.citizen=Client(base)
        self.citizen.login('citizen@civicserve.local')
        self.officer=Client(base)
        self.officer.login('officer@civicserve.local')
        self.admin=Client(base)
        self.admin.login('admin@civicserve.local')

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        app.DB_PATH=self.saved_path
        self.temp.cleanup()

    def test_no_access_without_login_and_no_csrf(self):
        code,_=self.public.request('/applications')
        self.assertEqual(code,401)
        code,_=self.public.request('/auth/register',method='POST',body={},csrf=False)
        self.assertEqual(code,403)

    def test_role_cannot_escalate_on_registration(self):
        code,body=self.public.post('/auth/register',{'name':'New Citizen','email':'new@example.com','password':'GoodPassword123','role':'admin'})
        self.assertEqual(code,200)
        self.assertEqual(body['user']['role'],'citizen')
        code,_=self.public.request('/audit')
        self.assertEqual(code,403)
        code,_=self.citizen.request('/officers')
        self.assertEqual(code,403)

    def test_end_to_end_review_and_reply(self):
        services=self.citizen.get('/services')['services']
        code,created=self.citizen.post('/applications',{
            'service_id':services[0]['id'],'subject':'Please certify the record','description':'I need a certified copy of the original record.',
            'address':'New Street, Ward 08, Test City'})
        self.assertEqual(code,200)
        case_id=created['id']
        self.assertTrue(created['reference'].startswith('CIV-'))
        code,_=self.citizen.request(f'/applications/{case_id}/transition',method='POST',body={'action':'approve'})
        self.assertEqual(code,403)
        code,_=self.officer.request(f'/applications/{case_id}')
        self.assertEqual(code,403)
        code,_=self.officer.post(f'/applications/{case_id}/transition',{'action':'start'})
        self.assertEqual(code,403)
        code,result=self.admin.post(f'/applications/{case_id}/assign',{'officer_id':2})
        self.assertEqual(code,200,result)
        code,_=self.officer.post(f'/applications/{case_id}/transition',{'action':'approve'})
        self.assertEqual(code,409)  # officer must start review first
        code,result=self.officer.post(f'/applications/{case_id}/transition',{'action':'start'})
        self.assertEqual(code,200,result)
        code,result=self.officer.post(f'/applications/{case_id}/transition',{'action':'needs_info','note':'Please explain the corrected date.'})
        self.assertEqual(code,200,result)
        code,_=self.officer.post(f'/applications/{case_id}/transition',{'action':'approve'})
        self.assertEqual(code,409)
        code,result=self.citizen.post(f'/applications/{case_id}/reply',{'message':'The corrected date is on the attached municipal record.'})
        self.assertEqual(code,200,result)
        code,result=self.officer.post(f'/applications/{case_id}/transition',{'action':'approve','note':'Verified record details.'})
        self.assertEqual(code,200,result)
        self.assertEqual(result['status'],'approved')
        code,_=self.admin.post(f'/applications/{case_id}/assign',{'officer_id':3})
        self.assertEqual(code,409)
        detail=self.citizen.get(f'/applications/{case_id}')
        self.assertEqual(detail['application']['status'],'approved')
        self.assertEqual(len(detail['timeline']),6)
        self.assertTrue(any('approved' in x['message'] for x in self.citizen.get('/notifications')['notifications']))
        self.assertTrue(any(x['action']=='application.approve' and x['application_id']==case_id for x in self.admin.get('/audit')['logs']))

    def test_other_citizen_cannot_read_or_reply_to_case(self):
        stranger=Client(self.public.base)
        stranger.login('meera@example.local')
        code,_=stranger.request('/applications/1')
        self.assertEqual(code,403)
        code,_=stranger.post('/applications/3/reply',{'message':'Unauthorised response to someone else case'})
        self.assertEqual(code,403)
        self.assertTrue(all(c['citizen_name']=='Meera Patel' for c in stranger.get('/applications')['applications']))

    def test_officer_creation_admin_only(self):
        payload={'name':'New Officer','email':'new.officer@example.local','password':'SecretPassword2026'}
        code,_=self.citizen.post('/officers',payload)
        self.assertEqual(code,403)
        code,result=self.admin.post('/officers',payload)
        self.assertEqual(code,200,result)
        officer=Client(self.public.base)
        account=officer.login('new.officer@example.local','SecretPassword2026')
        self.assertEqual(account['role'],'officer')

    def test_validation_and_notifications_owned(self):
        code,_=self.citizen.post('/applications',{'service_id':1,'subject':'x','description':'short','address':'x'})
        self.assertEqual(code,400)
        notices=self.citizen.get('/notifications')['notifications']
        self.assertTrue(notices)
        notice_id=notices[0]['id']
        code,_=self.officer.post(f'/notifications/{notice_id}/read')
        self.assertEqual(code,404)
        code,_=self.citizen.post('/notifications/read-all')
        self.assertEqual(code,200)
        self.assertTrue(all(x['is_read'] for x in self.citizen.get('/notifications')['notifications']))

    def test_login_and_csrf_protection(self):
        code,_=self.public.post('/auth/login',{'email':'citizen@civicserve.local','password':'wrong'})
        self.assertEqual(code,401)
        code,_=self.citizen.request('/auth/logout',method='POST',body={},csrf=False)
        self.assertEqual(code,403)
        code,_=self.citizen.post('/auth/logout')
        self.assertEqual(code,200)
        code,_=self.citizen.request('/applications')
        self.assertEqual(code,401)

    def test_public_health_and_assets(self):
        self.assertTrue(self.public.get('/health')['ok'])
        with self.public.opener.open(self.public.base+'/') as response:
            self.assertIn(b'CivicServe',response.read())
        with self.public.opener.open(self.public.base+'/app.js') as response:
            self.assertIn(b'showDashboard',response.read())

if __name__=='__main__':
    unittest.main()
