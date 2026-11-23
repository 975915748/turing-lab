import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from app import create_app, seed_admin


@pytest.fixture
def env(tmp_path):
    db=tmp_path/'test.sqlite3'
    seed_admin(db,'root_admin','Game Master','a-safe-admin-pass')
    app=create_app(db)
    admin=TestClient(app)
    guest1=TestClient(app)
    guest2=TestClient(app)
    stranger=TestClient(app)
    def post(client,route,data=None,expected=200):
        csrf = client.get('/api/me')
        token = csrf.json()['user']['csrf'] if csrf.status_code==200 else None
        headers={'X-CSRF-Token':token} if token else {}
        r=client.post(route,json=data or {},headers=headers)
        assert r.status_code==expected, f'{route}: {r.status_code} {r.text}'
        return r.json()
    post(admin,'/api/login',{'username':'root_admin','password':'a-safe-admin-pass'})
    room=post(admin,'/api/rooms',{'title':'实验 · 首轮','judge':'盲测裁判'})
    code=room['code']
    for i,client in enumerate((guest1,guest2),1):
        post(client,'/api/register',{'username':f'player_{i}','display_name':f'真人姓名_{i}',
                                    'password':f'password_{i}test','code':code})
    return app,db,admin,guest1,guest2,stranger,post,room['room_id'],code


def test_full_round_security_and_reveal(env):
    app,db,admin,p1,p2,outsider,post,rid,code=env
    base=f'/api/rooms/{rid}'
    assert len(p1.get(base).json()['humans'])==2
    assert p1.get(base).json()['mine']=='A'
    assert p2.get(base).json()['mine']=='B'
    assert p1.get(base).json()['humans'][1]['name']=='真人姓名_2'
    assert outsider.get(base).status_code==401
    ai=post(admin,base+'/ai',{'name':'ModelZ'})
    assert ai['alias']=='C'
    # players cannot run administrator-only endpoints
    post(p1,base+'/ai',{'name':'hack'},403)
    with TestClient(app) as browser:
        r=browser.get('/api/health')
        assert r.json()=={'ok':True}
        assert '图灵实验室' in browser.get('/').text
        assert browser.get('/static/app.js').status_code==200
    # WebSocket notifications are sent without any hidden contents
    with p1.websocket_connect(f'/ws/rooms/{rid}',headers={'origin':'http://testserver'}) as ws:
        assert ws.receive_json()['type']=='connected'
        post(admin,base+'/question',{'text':'你会怎样躲开光球？'})
        message=ws.receive_json()
        assert message['reason']=='judge_question'
        assert '光球' not in str(message)
    assert p2.get(base).json()['rounds'][0]['question']=='你会怎样躲开光球？'
    post(p1,base+'/suggestions',{'text':'我想追问辐光出招权重'})
    assert len(p2.get(base).json()['suggestions'])==1
    # no new participants once first judge question is posted
    post(outsider,'/api/register',{'username':'player_3','display_name':'第三位',
                                  'password':'passwordtest','code':code},409)
    post(p1,base+'/answers',{'text':'秘密回答_ALPHA'})
    post(admin,base+'/ai/C/answer',{'text':'秘密AI回答_C'})
    public=p2.get(base).json()
    assert public['rounds'][0]['submitted']==['A','C']
    assert public['rounds'][0]['answers']=={}
    assert public['verdict'] is None
    assert p1.get(base).json()['rounds'][0]['answers']['A']['text']=='秘密回答_ALPHA'
    assert admin.get(base).json()['rounds'][0]['answers']=={}
    # log cannot be used to read other unrevealed answers
    for client in (admin,p2):
        log=client.get(base+'/rounds/1/log')
        assert log.status_code==200
        assert '秘密回答_ALPHA' not in log.text
        assert '秘密AI回答_C' not in log.text
        assert '（已提交，待统一揭晓）' in log.text
    post(admin,base+'/packets',{},409)
    post(p1,base+'/answers',{'text':'第二次回答'},409)
    # invalid CSRF token
    assert p1.post(base+'/suggestions',json={'text':'CSRF attempt'}).status_code==403
    post(p2,base+'/answers',{'text':'秘密回答_B'})
    released=p1.get(base).json()['rounds'][0]
    assert released['status']=='revealed'
    assert released['answers']['B']['text']=='秘密回答_B'
    assert '秘密AI回答_C' in p2.get(base+'/rounds/1/log').text
    # judge packets contain ONLY aliases and answer text; no human identity metadata
    packet=post(admin,base+'/packets',{})
    content=admin.get(base+f'/packets/{packet["packet_id"]}').json()['content']
    assert '【参与者 A · 原始回答】' in content
    assert '【参与者 C · 原始回答】' in content
    assert '真人姓名_1' not in content
    assert '真人姓名_2' not in content
    assert 'ModelZ' not in content
    assert 'player_1' not in content
    for human in admin.get(base).json()['humans']:
        assert human['uid'] not in content
    assert 'Human' not in content and 'AI ·' not in content
    assert p1.get(base+f'/packets/{packet["packet_id"]}').status_code==403
    post(admin,base+f'/packets/{packet["packet_id"]}/sent')
    old=admin.get(base+f'/packets/{packet["packet_id"]}').json()['content']
    post(admin,base+'/next')
    assert p2.get(base).json()['current_round']==2
    assert admin.get(base+f'/packets/{packet["packet_id"]}').json()['content']==old
    post(admin,base+'/question',{'text':'新的追问'})
    post(p1,base+'/answers',{'text':'回复1'})
    post(p2,base+'/answers',{'text':'回复2'})
    post(admin,base+'/ai/C/answer',{'text':'回复3'})
    packet2=post(admin,base+'/packets',{})
    text2=admin.get(base+f'/packets/{packet2["packet_id"]}').json()['content']
    assert '范围：仅第 2 轮' in text2
    assert '秘密回答_ALPHA' not in text2 and '回复2' in text2
    post(admin,base+'/verdict',{'predictions':{'A':'Human','B':'AI','C':'AI'},'confidence':80,'evidence':'判断依据'},200)
    before=p1.get(base).json()
    assert before['verdict_locked'] is True
    assert before['verdict'] is None
    post(admin,base+'/next',{},409)
    post(admin,base+'/reveal')
    after=p1.get(base).json()
    assert after['status']=='finished'
    assert after['verdict']['confidence']==80
    assert after['verdict']['predictions']['B']=='AI'
    assert after['revealed_at']
    post(p2,base+'/answers',{'text':'after reveal'},409)


def test_join_and_cross_room_privacy(env):
    app,db,admin,p1,p2,outsider,post,rid,code=env
    other=post(admin,'/api/rooms',{'title':'另一间房'})
    oid=other['room_id']
    assert p1.get(f'/api/rooms/{oid}').status_code==403
    assert p1.get(f'/api/rooms/{rid}').json()['invite_code'] is None
    post(p1,'/api/join',{'code':other['code']})
    assert p1.get(f'/api/rooms/{oid}').status_code==200
    assert p1.get(f'/api/rooms/{rid}').status_code==200
    assert p1.get('/api/me').json()['user']['is_admin']==0
    owned=post(p1,'/api/rooms',{'title':'自己的房间'})
    assert p1.get(f'/api/rooms/{owned["room_id"]}').json()['can_manage']
    post(p1,f'/api/rooms/{rid}/question',{'text':'not allowed'},403)
    # Only another domain's Origin is rejected for state mutations
    token=p1.get('/api/me').json()['user']['csrf']
    assert p1.post('/api/join',json={'code':code},headers={
        'origin':'https://evil.example','X-CSRF-Token':token}).status_code==403
    with pytest.raises(Exception):
        with p1.websocket_connect(f'/ws/rooms/{rid}',headers={'origin':'https://evil.example'}):
            pass
    post(p1,'/api/logout',{})
    assert p1.get(f'/api/rooms/{rid}').status_code==401


def test_invalid_invite_and_admin_setup(tmp_path):
    path=tmp_path/'g.db'
    assert seed_admin(path,'admin_123','主持人','supersecret')
    assert seed_admin(path,'admin_123','主持人','newpass123') is False
    with TestClient(create_app(path)) as c:
        assert c.post('/api/register',json={'username':'hello_1','display_name':'hi','password':'12345678','code':'INVALID'}).status_code==404
    # passwords aren't stored as plaintext
    import sqlite3
    with sqlite3.connect(path) as db:
        row=db.execute('SELECT pw_hash FROM users WHERE is_admin=1').fetchone()[0]
        assert row.startswith('pbkdf2_sha256$') and 'supersecret' not in row

