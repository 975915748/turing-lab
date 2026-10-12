"""多裁判互相隔离/升级兼容测试。"""
import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from app import create_app, seed_admin


def post(client, path, data=None, status=200):
    me=client.get('/api/me')
    headers={'X-CSRF-Token':me.json()['user']['csrf']} if me.status_code == 200 else {}
    r=client.post(path,json=data or {},headers=headers)
    assert r.status_code==status,(path,r.status_code,r.text)
    return r.json()


def test_two_judges_questions_verdicts_and_privacy(tmp_path):
    path=tmp_path/'game.sqlite3'
    seed_admin(path,'root_01','Host Root','pass_admin123')
    app=create_app(path)
    admin,p1,p2,j1,j2,other=[TestClient(app) for _ in range(6)]
    post(admin,'/api/login',{'username':'root_01','password':'pass_admin123'})
    room=post(admin,'/api/rooms',{'title':'匿名多裁判','judge':'旧裁判'})
    rid=room['room_id']; base=f'/api/rooms/{rid}'
    for i,client in ((1,p1),(2,p2)):
        post(client,'/api/register',{'username':f'human_{i}', 'password':'human_pass123',
                                    'display_name':f'真人昵称_{i}','code':room['code']})
    post(admin,base+'/ai',{'name':'my_secret_ai_model'})
    for label in ('judge_01','judge_02'):
        post(admin,base+'/judges',{'username':label,'display_name':f'裁判 {label}',
                                  'password':f'{label}-password'})
    post(j1,'/api/login',{'username':'judge_01','password':'judge_01-password'})
    post(j2,'/api/login',{'username':'judge_02','password':'judge_02-password'})
    assert j1.get('/api/me').json()['rooms'][0]['role']=='judge'
    judge_view=j1.get(base).json()
    assert judge_view['aliases']==['A','B','C']
    assert judge_view['is_judge'] and 'humans' not in judge_view
    assert 'ais' not in judge_view and 'invite_code' not in judge_view
    assert 'human_1' not in json.dumps(judge_view,ensure_ascii=False)
    assert 'my_secret_ai_model' not in json.dumps(judge_view,ensure_ascii=False)
    assert p1.get(base).json()['humans'][0]['username']=='human_1'
    post(j1,'/api/join',{'code':room['code']},409)
    post(admin,base+'/judges/assign',{'username':'human_1','label':'闯入裁判'},409)
    post(p1,base+'/judge-questions',{'text':'违规'},403)
    post(j1,base+'/ai',{'name':'违规'},403)
    post(j1,base+'/question',{'text':'违规'},403)
    with j1.websocket_connect(f'/ws/rooms/{rid}',headers={'origin':'http://testserver'}) as ws:
        assert ws.receive_json()['type']=='connected'
        post(j1,base+'/judge-questions',{'text':'你如何识别黄昏中的灯光？'})
        event=ws.receive_json()
        assert event['reason']=='judge_question_proposed'
        assert '灯光' not in json.dumps(event,ensure_ascii=False)
    post(j2,base+'/judge-questions',{'text':'你会如何形容春风？'})
    post(j1,base+'/judge-questions',{'text':'重复问题'},409)
    assert len(p2.get(base).json()['rounds'][0]['questions'])==2
    assert p2.get(base).json()['rounds'][0]['question']==''
    post(admin,base+'/ai',{'name':'后来修改阵容'},409)
    post(admin,base+'/start')
    assert '黄昏中的灯光' in p1.get(base).json()['rounds'][0]['question']
    post(j1,base+'/judge-questions',{'text':'迟来新题'},409)
    post(admin,base+'/verdict',{'predictions':{'A':'Human','B':'Human','C':'AI'},'confidence':60,'evidence':'非法代交'},409)
    post(j1,base+'/judge-verdict',{'predictions':{'A':'AI','B':'Human','C':'AI'},'confidence':60,'evidence':'提前锁定'},409)
    post(p1,base+'/answers',{'text':'秘密答案_人类A'})
    assert '秘密答案_人类A' not in json.dumps(j1.get(base).json(),ensure_ascii=False)
    assert '秘密答案_人类A' not in j1.get(base+'/rounds/1/log').text
    assert '秘密答案_人类A' not in json.dumps(j2.get(base).json(),ensure_ascii=False)
    post(p2,base+'/answers',{'text':'秘密答案_人类B'})
    post(admin,base+'/ai/C/answer',{'text':'秘密答案_AI'})
    # Realtime answer release to all judges after everybody replies.
    for client in (p1,p2,j1,j2):
        view=client.get(base).json()
        assert view['rounds'][0]['status']=='revealed'
        assert view['rounds'][0]['answers']['A']['text']=='秘密答案_人类A'
    # Judge can get anonymous logs but no identity mapping or peer verdict.
    anon_log=j2.get(base+'/rounds/1/log').text
    assert '秘密答案_AI' in anon_log
    assert '真人昵称' not in anon_log and 'human_1' not in anon_log
    assert 'my_secret_ai_model' not in anon_log
    assert j2.get(base+'/packets/1').status_code==403
    post(j1,base+'/judge-verdict',{'predictions':{'A':'Human','B':'AI','C':'AI'},
                  'confidence':97,'evidence':'独立秘密判断 J1','weakness':'另一个秘密字段'},200)
    post(j1,base+'/judge-verdict',{'predictions':{'A':'AI','B':'AI','C':'AI'},'confidence':1,'evidence':'篡改'},409)
    for other_user in (j2,p1,p2,admin):
        assert '独立秘密判断 J1' not in json.dumps(other_user.get(base).json(),ensure_ascii=False)
    assert '独立秘密判断 J1' in json.dumps(j1.get(base).json(),ensure_ascii=False)
    post(admin,base+'/reveal',{},409)
    post(admin,base+'/next',{},409)
    post(admin,base+'/judges',{'username':'judge_late','display_name':'迟到','password':'123456789'},409)
    post(j2,base+'/judge-verdict',{'predictions':{'A':'AI','B':'AI','C':'Human'},
                                  'confidence':49,'evidence':'独立秘密判断 J2'},200)
    assert '独立秘密判断 J2' not in json.dumps(j1.get(base).json(),ensure_ascii=False)
    post(admin,base+'/reveal')
    assert len(admin.get(base).json()['verdicts'])==2
    assert '独立秘密判断 J1' in json.dumps(p1.get(base).json(),ensure_ascii=False)
    assert '独立秘密判断 J2' in json.dumps(p1.get(base).json(),ensure_ascii=False)
    for client in (j1,j2):
        view=client.get(base).json()
        assert 'verdicts' not in view and 'humans' not in view and 'ais' not in view
        assert '真人昵称' not in json.dumps(view,ensure_ascii=False)
        assert 'my_secret_ai_model' not in json.dumps(view,ensure_ascii=False)
    assert '独立秘密判断 J2' not in json.dumps(j1.get(base).json(),ensure_ascii=False)
    assert '独立秘密判断 J1' not in json.dumps(j2.get(base).json(),ensure_ascii=False)
    assert j1.get(base+'/rounds/1/log').status_code==200
    # Contextual routes do not give privilege to a judge from a different room.
    other_room=post(admin,'/api/rooms',{'title':'秘密房间'})
    assert j1.get(f'/api/rooms/{other_room["room_id"]}').status_code==403
    assert other.get(base).status_code==401


def test_existing_user_can_be_judge_for_another_room(tmp_path):
    db=tmp_path/'db.sqlite3';seed_admin(db,'the_admin','主持','admin_pass123')
    app=create_app(db);admin=TestClient(app);other=TestClient(app)
    post(admin,'/api/login',{'username':'the_admin','password':'admin_pass123'})
    first=post(admin,'/api/rooms',{'title':'A房间'})
    post(other,'/api/register',{'username':'multi_user','display_name':'X','password':'12345678','code':first['code']})
    second=post(admin,'/api/rooms',{'title':'B房间'})
    post(admin,f'/api/rooms/{second["room_id"]}/judges/assign',{'username':'multi_user','label':'匿名裁判'})
    judge_view=other.get(f'/api/rooms/{second["room_id"]}').json()
    assert judge_view['is_judge']
    assert other.get(f'/api/rooms/{first["room_id"]}').json()['mine']=='A'


def test_legacy_database_auto_migrates_and_old_room_retained(tmp_path):
    # Make an original-schema database without the new independent judge tables.
    import sqlite3
    from app import hash_password
    db=tmp_path/'existing.sqlite3'
    with sqlite3.connect(db) as c:
        c.executescript("""
            CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT UNIQUE COLLATE NOCASE,
                display_name TEXT, pw_hash TEXT, is_admin INTEGER, created_at TEXT);
            CREATE TABLE rooms(id INTEGER PRIMARY KEY, title TEXT, code TEXT UNIQUE,
                judge TEXT, status TEXT, current_round INTEGER,
                verdict_json TEXT, verdict_locked_at TEXT, revealed_at TEXT, created_at TEXT);
            CREATE TABLE rounds(id INTEGER PRIMARY KEY,room_id INTEGER,number INTEGER,
                question TEXT,status TEXT,created_at TEXT,revealed_at TEXT,
                UNIQUE(room_id,number));
        """)
        c.execute('INSERT INTO users (username,display_name,pw_hash,is_admin,created_at) VALUES(?,?,?,?,?)',
                  ('original_admin','原管理员',hash_password('secure-password123'),1,'2026-01-01T00:00:00+00:00'))
        c.execute('INSERT INTO rooms (title,code,judge,status,current_round,created_at) VALUES (?,?,?,?,?,?)',
                  ('旧房间','OLDROOMAB','旧外部裁判','open',1,'2026-01-01T00:00:00+00:00'))
        c.execute('INSERT INTO rounds (room_id,number,question,status,created_at) VALUES (?,?,?,?,?)',
                  (1,1,'','draft','2026-01-01T00:00:00+00:00'))
    new_app=create_app(db)
    backups=list(tmp_path.glob('existing.pre-v1_1-*.sqlite3'))
    assert len(backups)==1 and backups[0].stat().st_size>0
    # A second startup should not generate a second migration backup.
    create_app(db)
    assert len(list(tmp_path.glob('existing.pre-v1_1-*.sqlite3')))==1
    newer=TestClient(new_app)
    post(newer,'/api/login',{'username':'original_admin','password':'secure-password123'})
    view=newer.get('/api/rooms/1').json()
    assert view['title']=='旧房间'
    assert view['judge']=='旧外部裁判'
    assert view['rounds'][0]['status']=='draft'
    assert view['judges']==[]
    post(newer,'/api/rooms/1/judges',{'username':'new_judge','display_name':'新裁判','password':'strongpass'},200)
    assert len(newer.get('/api/rooms/1').json()['judges'])==1
