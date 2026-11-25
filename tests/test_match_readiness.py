"""Match readiness must mirror the authoritative start-round checks."""
from fastapi.testclient import TestClient

from app import create_app, db_open, seed_admin


def post(client, path, body=None, expected=200):
    me = client.get('/api/me')
    headers = {'X-CSRF-Token': me.json()['user']['csrf']} if me.status_code == 200 else {}
    response = client.post(path, json=body or {}, headers=headers)
    assert response.status_code == expected, (path, response.status_code, response.text)
    return response.json() if response.content else {}


def make_players(app, code, count=2):
    clients = []
    for index in range(count):
        client = TestClient(app)
        post(client, '/api/register', {
            'username': f'readiness_player_{index}',
            'display_name': f'参赛者 {index + 1}',
            'password': 'readiness-password',
            'code': code,
        })
        clients.append(client)
    return clients


def add_ai_judge(client, base, name):
    return post(client, base + '/ai-judges', {'name': name})


def submit_ai_question(client, base, judge_id, text):
    return post(client, f'{base}/ai-judges/{judge_id}/questions', {'text': text})


def make_lab(tmp_path):
    path = tmp_path / 'readiness.sqlite3'
    seed_admin(path, 'readiness_admin', '准备检查管理员', 'readiness-admin-password')
    app = create_app(path)
    admin = TestClient(app)
    post(admin, '/api/login', {
        'username': 'readiness_admin',
        'password': 'readiness-admin-password',
    })
    room = post(admin, '/api/rooms', {'title': '开局准备检查'})
    return {'app': app, 'path': path, 'admin': admin,
            'base': f"/api/rooms/{room['room_id']}",
            'room_id': room['room_id'], 'code': room['code']}


def test_zero_one_two_players_and_external_judge_manual_flow(tmp_path):
    lab = make_lab(tmp_path)
    admin, base = lab['admin'], lab['base']

    readiness = admin.get(base).json()['readiness']
    assert readiness['participant_count'] == 0
    assert readiness['external_judge'] is True
    assert readiness['manual_question_required'] is True
    assert readiness['suggested_view'] == 'admin'
    assert any('0 位' in item for item in readiness['blockers'])
    post(admin, base + '/start', expected=409)

    make_players(lab['app'], lab['code'], 1)
    readiness = admin.get(base).json()['readiness']
    assert readiness['participant_count'] == 1
    assert readiness['suggested_view'] == 'admin'
    assert any('1 位' in item for item in readiness['blockers'])
    post(admin, base + '/start', expected=409)

    second = TestClient(lab['app'])
    post(second, '/api/register', {
        'username': 'readiness_player_second',
        'display_name': '参赛者 2',
        'password': 'readiness-password',
        'code': lab['code'],
    })
    readiness = admin.get(base).json()['readiness']
    assert readiness['participant_count'] == 2
    assert readiness['external_judge'] is True
    assert readiness['manual_question_required'] is True
    assert readiness['can_start'] is False

    # 外部裁判不需要独立裁判席或 API 配置，原手动发布入口保持可用。
    post(admin, base + '/question', {'text': '外部裁判提交的本轮问题'})
    room = admin.get(base).json()
    assert room['rounds'][0]['status'] == 'collecting'
    assert room['rounds'][0]['question'] == '外部裁判提交的本轮问题'
    assert room['model_request_count'] == 0


def test_one_submitted_question_allows_host_to_start_with_other_judge_pending(tmp_path):
    lab = make_lab(tmp_path)
    admin, base = lab['admin'], lab['base']
    make_players(lab['app'], lab['code'], 2)
    first = add_ai_judge(admin, base, '已交题 AI 裁判')
    add_ai_judge(admin, base, '尚未交题的手动 AI 裁判')
    submit_ai_question(admin, base, first['id'], '第一份有效裁判问题')

    readiness = admin.get(base).json()['readiness']
    assert readiness['question_count'] == 1
    assert readiness['total_judges'] == 2
    assert readiness['submitted_judges'] == 1
    assert readiness['manual_pending'] == 1
    assert readiness['can_start'] is True

    post(admin, base + '/start')
    assert admin.get(base).json()['rounds'][0]['status'] == 'collecting'


def test_unbound_ai_judge_does_not_block_existing_manual_question(tmp_path):
    lab = make_lab(tmp_path)
    admin, base = lab['admin'], lab['base']
    make_players(lab['app'], lab['code'], 2)
    submitted = add_ai_judge(admin, base, '手动裁判')
    add_ai_judge(admin, base, '未绑定模型的 AI 裁判')
    submit_ai_question(admin, base, submitted['id'], '可用的手动问题')

    readiness = admin.get(base).json()['readiness']
    assert readiness['can_start'] is True
    assert readiness['manual_pending'] == 1
    post(admin, base + '/start')
    assert admin.get(base).json()['rounds'][0]['status'] == 'collecting'


def test_combined_questions_over_6000_chars_block_start(tmp_path):
    lab = make_lab(tmp_path)
    admin, base = lab['admin'], lab['base']
    make_players(lab['app'], lab['code'], 2)
    judges = [add_ai_judge(admin, base, f'裁判 {index}') for index in range(3)]
    # Simulate legacy / manually seeded oversized data; current submission routes
    # reject a question before it can push the combined total past 6000 characters.
    with db_open(lab['path']) as db:
        for judge in judges:
            db.execute('''INSERT INTO ai_judge_questions(
                room_id,round_number,ai_judge_id,text,created_at)
                VALUES(?,1,?,?,?)''',
                (lab['room_id'],judge['id'],'题' * 3000,'2026-11-25T00:00:00+00:00'))

    readiness = admin.get(base).json()['readiness']
    assert readiness['question_count'] == 3
    assert readiness['question_chars'] > 6000
    assert readiness['can_start'] is False
    assert any('6000' in item for item in readiness['blockers'])
    post(admin, base + '/start', expected=409)



def test_manual_ai_question_submission_rejects_combined_overflow(tmp_path):
    lab = make_lab(tmp_path)
    admin, base = lab['admin'], lab['base']
    make_players(lab['app'], lab['code'], 2)
    first = add_ai_judge(admin, base, '手动裁判 A')
    second = add_ai_judge(admin, base, '手动裁判 B')
    third = add_ai_judge(admin, base, '手动裁判 C')

    submit_ai_question(admin, base, first['id'], '甲' * 2980)
    submit_ai_question(admin, base, second['id'], '乙' * 2980)
    post(admin, f"{base}/ai-judges/{third['id']}/questions",
         {'text': '丙' * 30}, expected=422)

    room = admin.get(base).json()
    assert len(room['rounds'][0]['questions']) == 2
    assert room['readiness']['question_chars'] <= 6000

def test_removed_and_soft_deleted_accounts_do_not_count_as_present_players(tmp_path):
    lab = make_lab(tmp_path)
    admin, base = lab['admin'], lab['base']
    make_players(lab['app'], lab['code'], 2)
    with db_open(lab['path']) as db:
        second_id=db.execute("SELECT id FROM users WHERE username='readiness_player_1'").fetchone()['id']
        db.execute("UPDATE members SET removed_at='2026-11-25T00:00:00+00:00' WHERE room_id=? AND user_id=?",
                   (lab['room_id'],second_id))
    assert admin.get(base).json()['readiness']['participant_count'] == 1
    post(admin, base + '/start', expected=409)

    with db_open(lab['path']) as db:
        first_id=db.execute("SELECT id FROM users WHERE username='readiness_player_0'").fetchone()['id']
        db.execute("UPDATE users SET deleted_at='2026-11-25T00:00:00+00:00' WHERE id=?", (first_id,))
    assert admin.get(base).json()['readiness']['participant_count'] == 0
