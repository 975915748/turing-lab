"""v1.2 真人+AI混合裁判：转录、盲测、锁定、迁移以及权限隔离。"""
import json
import re
import sqlite3
from pathlib import Path
from fastapi.testclient import TestClient
from app import create_app, seed_admin


def call(c, url, data=None, expected=200):
    m = c.get('/api/me')
    headers = {'X-CSRF-Token': m.json()['user']['csrf']} if m.status_code == 200 else {}
    r = c.post(url, json=data or {}, headers=headers)
    assert r.status_code == expected, (url, r.status_code, r.text)
    return r.json()


def verdict(predictions, label):
    return {'predictions': predictions, 'evidence': label, 'confidence': 62,
            'weakness': '需要更多轮次', 'possible_error': '时间不足'}


def test_mixed_judges_secrets_and_reveal(tmp_path):
    p = tmp_path / 'game.sqlite3'
    seed_admin(p, 'admin_12', '负责人', 'password1234')
    a = create_app(p)
    admin, p1, p2, human_judge = [TestClient(a) for _ in range(4)]
    call(admin, '/api/login', {'username': 'admin_12', 'password': 'password1234'})
    room = call(admin, '/api/rooms', {'title': '测试混合裁判'})
    base = f'/api/rooms/{room["room_id"]}'
    for i,c in enumerate([p1,p2]):
        call(c, '/api/register', {'username': f'player_1{i}', 'display_name': f'本人_{i}',
              'password': 'password1234', 'code': room['code']})
    call(admin, base+'/ai', {'name':'participant_ai'})
    human_uid=call(admin, base+'/judges', {'username': 'judge001', 'display_name':'真人裁判一号',
         'password':'password1234'})['uid']
    aj1 = call(admin, base+'/ai-judges', {'name':'AI 裁判甲'})['id']
    aj2 = call(admin, base+'/ai-judges', {'name':'AI 裁判乙'})['id']
    call(human_judge, '/api/login', {'uid':human_uid, 'password':'password1234'})
    info = admin.get(base).json()
    assert len(info['judges']) == 3
    assert [j['kind'] for j in info['judges']].count('ai') == 2
    assert all(j['username'] is None for j in info['judges'] if j['kind']=='ai')
    assert len(info['humans']) == 2
    # Non-admin cannot create / transcribe AI judge content.
    call(p1, base+'/ai-judges', {'name':'伪裁判'}, 403)
    call(p1, base+f'/ai-judges/{aj1}/questions', {'text':'伪裁判问题'}, 403)
    call(human_judge, base+f'/ai-judges/{aj1}/questions', {'text':'冒充 AI 裁判'}, 403)
    call(admin, base+f'/ai-judges/{aj1}/questions', {'text':'AI 甲如何识别雨声？'})
    call(admin, base+f'/ai-judges/{aj2}/questions', {'text':'AI 乙如何识别诗歌？'})
    call(human_judge, base+'/judge-questions', {'text':'真人裁判：谈谈记忆。'})
    call(admin, base+f'/ai-judges/{aj1}/questions', {'text':'重复'}, 409)
    qs=admin.get(base).json()['rounds'][0]['questions']
    assert len(qs)==3
    assert {q['kind'] for q in qs}=={'human','ai'}
    assert {q['judge_id'] for q in qs if q['kind']=='ai'}=={aj1,aj2}
    with human_judge.websocket_connect(f'/ws/rooms/{room["room_id"]}',headers={'origin':'http://testserver'}) as ws:
        assert ws.receive_json()['type']=='connected'
        call(admin,base+'/start')
        assert ws.receive_json()['reason']=='judge_question'
    assert all(x in admin.get(base).json()['rounds'][0]['question'] for x in ('AI 甲','AI 乙','真人裁判'))
    call(admin,base+f'/ai-judges/{aj2}/questions',{'text':'迟到了'},409)
    for i,c in enumerate([p1,p2]):
        call(c,base+'/answers',{'text':f'私密答案_{i}'})
    assert '私密答案_0' not in json.dumps(human_judge.get(base).json(),ensure_ascii=False)
    call(admin,base+'/ai/C/answer',{'text':'私密答案_AI'})
    assert '私密答案_AI' in json.dumps(human_judge.get(base).json(),ensure_ascii=False)
    call(admin,base+f'/ai-judges/{aj1}/verdict',verdict({'A':'Human','B':'AI','C':'AI'},'AI甲的私密判断'))
    call(admin,base+f'/ai-judges/{aj1}/verdict',verdict({'A':'Human','B':'Human','C':'AI'},'覆写'),409)
    call(p1,base+f'/ai-judges/{aj2}/verdict',verdict({'A':'Human','B':'AI','C':'AI'},'攻击'),403)
    for c in (p1,p2,human_judge,admin):
        resp=c.get(base)
        assert 'AI甲的私密判断' not in json.dumps(resp.json(),ensure_ascii=False)
        assert 'AI甲的私密判断' not in c.get(base+'/rounds/1/log').text
    call(admin,base+'/reveal',{},409)
    call(admin,base+'/next',{},409)
    call(human_judge,base+'/judge-verdict',verdict({'A':'Human','B':'Human','C':'AI'},'真人秘密答案'))
    # Distinct verdicts remain unreadable to each other; even the judge's own view is anonymized.
    assert 'AI甲的私密判断' not in json.dumps(human_judge.get(base).json(),ensure_ascii=False)
    assert '真人秘密答案' not in json.dumps(p1.get(base).json(),ensure_ascii=False)
    call(admin,base+'/reveal',{},409)
    call(admin,base+f'/ai-judges/{aj2}/verdict',verdict({'A':'AI','B':'Human','C':'Human'},'AI乙的独立判断'))
    call(admin,base+'/reveal')
    results=p1.get(base).json()['verdicts']
    assert len(results)==3
    assert {v['judge'] for v in results}=={'真人裁判一号','AI 裁判甲','AI 裁判乙'}
    assert {v['kind'] for v in results}=={'human','ai'}
    real_view=human_judge.get(base).json()
    assert 'verdicts' not in real_view
    assert 'AI甲的私密判断' not in json.dumps(real_view,ensure_ascii=False)
    assert 'AI乙的独立判断' not in json.dumps(real_view,ensure_ascii=False)
    assert '本人_0' not in json.dumps(real_view,ensure_ascii=False)
    assert 'participant_ai' not in json.dumps(real_view,ensure_ascii=False)
    assert '真人秘密答案' in json.dumps(real_view,ensure_ascii=False)


def test_ai_only_judges_and_old_database_migrates(tmp_path):
    db=tmp_path/'legacy.sqlite3'
    seed_admin(db,'admin008','管理员','password123')
    with sqlite3.connect(db) as c:
        c.execute('DROP TABLE ai_judge_verdicts')
        c.execute('DROP TABLE ai_judge_questions')
        c.execute('DROP TABLE ai_judges')
    a=create_app(db)
    assert len(list(tmp_path.glob('legacy.pre-v1_11-*.sqlite3')))==1
    create_app(db)
    assert len(list(tmp_path.glob('legacy.pre-v1_11-*.sqlite3')))==1
    admin=TestClient(a);one=TestClient(a);two=TestClient(a)
    call(admin,'/api/login',{'username':'admin008','password':'password123'})
    r=call(admin,'/api/rooms',{'title':'AI裁判房间'})
    base=f'/api/rooms/{r["room_id"]}'
    for i,c in enumerate((one,two)):
        call(c,'/api/register',{'username':f'player00{i}', 'password':'password123',
             'display_name':'人类玩家','code':r['code']})
    j=call(admin,base+'/ai-judges',{'name':'DeepSeek'})['id']
    assert admin.get(base).json()['judges'][0]['kind']=='ai'
    call(admin,base+'/question',{'text':'旧式直接录题'},409)
    call(admin,base+f'/ai-judges/{j}/questions',{'text':'AI 裁判提出的问题'})
    call(admin,base+'/start')
    call(one,base+'/answers',{'text':'回答1'})
    call(two,base+'/answers',{'text':'回答2'})
    call(admin,base+'/verdict',verdict({'A':'AI','B':'AI'},'旧模式偷跑'),409)
    call(admin,base+f'/ai-judges/{j}/verdict',verdict({'A':'AI','B':'Human'},'AI唯一裁判的回答'))
    call(admin,base+'/reveal')
    assert len(one.get(base).json()['verdicts'])==1
    assert one.get(base).json()['verdicts'][0]['judge']=='DeepSeek'
    assert 'AI唯一裁判的回答' in json.dumps(one.get(base).json(),ensure_ascii=False)


def test_ui_static_assets_versioned(tmp_path):
    app=create_app(tmp_path/'db.sqlite3')
    c=TestClient(app)
    homepage=c.get('/')
    assert homepage.status_code == 200 and homepage.headers['Cache-Control'] == 'no-store'
    script=re.search(r'src="(/static/app\.js\?v=[^"]+)"', homepage.text)
    assert script is not None
    response=c.get(script.group(1))
    assert response.status_code == 200
    js=response.text
    for field in ('add-ai-judge','ai-judge-question','ai-judge-verdict','human-judges','ai-judges','personal-center','公共聊天'):
        assert field in js
    assert '真人 / AI 裁判管理' not in js

