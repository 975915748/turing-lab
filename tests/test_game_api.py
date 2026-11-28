from fastapi.testclient import TestClient

from app import create_app


def post(client, path, data, expected=200):
    response = client.post('/game' + path, json=data)
    assert response.status_code == expected, response.text
    return response.json()


def test_game_page_and_api_flow(tmp_path):
    db_path = tmp_path / 'host.sqlite3'
    with TestClient(create_app(db_path)) as client:
        page = client.get('/')
        assert page.status_code == 200
        assert client.get('/game', follow_redirects=False).headers['location'] == '/'
        assert 'style.css?v=2.2.0' in page.text
        assert 'app.js?v=2.2.0' in page.text
        assert 'base: "/game"' in page.text
        assert 'server.py' not in page.text
        assert client.get('/game/api/health').json() == {'ok': True, 'rooms': 0, 'users': 0}
        assert client.get('/api/health').json() == {'ok': True}

        owner = post(client, '/api/register', {'name': '房主', 'pwd': 'owner-password'})
        post(client, '/api/register', {'name': '房主', 'pwd': 'owner-password'}, 409)
        assert post(client, '/api/login', {'name': '房主', 'pwd': 'wrong-password'}, 401)['error']
        assert post(client, '/api/login', {'name': '房主', 'pwd': 'owner-password'})['uid'] == owner['uid']

        created = post(client, '/api/createRoom', {
            'name': '网页对局', 'owner': owner['uid'], 'ownerName': '房主'
        })['room']
        code = created['id']
        assert created['mode'] == 'classic'
        assert client.get('/game/api/rooms', params={'uid': owner['uid']}).json()['rooms'][0]['id'] == code

        judge = post(client, '/api/register', {'name': '裁判', 'pwd': 'judge-password'})
        joined = post(client, '/api/join', {
            'code': code, 'uid': judge['uid'], 'name': '裁判', 'role': 'judge'
        })['room']
        assert joined['judges'][0]['uid'] == judge['uid']

        joined['pendingQ'] = ['哪一段回答更像真人？']
        joined['scores'] = {judge['uid']: 3}
        post(client, '/api/room', {'room': joined})
        saved = client.get('/game/api/room', params={'code': code}).json()['room']
        assert saved['pendingQ'] == ['哪一段回答更像真人？']
        board = client.get('/game/api/leaderboard').json()['leaderboard']
        assert board == [{'uid': judge['uid'], 'name': '裁判', 'score': 3.0, 'games': 1}]

        messages = [{'uid': owner['uid'], 'name': '房主', 'msg': '开始比赛', 'time': 1}]
        post(client, '/api/chat', {'code': code, 'chat': messages})
        assert client.get('/game/api/chat', params={'code': code}).json()['chat'] == messages

        post(client, '/api/profile', {'uid': owner['uid'], 'name': '新房主'})
        post(client, '/api/changePwd', {
            'uid': owner['uid'], 'oldPwd': 'owner-password', 'newPwd': 'new-owner-password'
        })
        assert post(client, '/api/login', {
            'name': '新房主', 'pwd': 'new-owner-password'
        })['uid'] == owner['uid']

        post(client, '/api/deleteRoom', {'code': code, 'uid': judge['uid']}, 403)
        post(client, '/api/deleteRoom', {'code': code, 'uid': owner['uid']})
        assert client.get('/game/api/room', params={'code': code}).status_code == 404


def test_game_state_uses_persistent_app_data_directory(tmp_path):
    db_path = tmp_path / 'host.sqlite3'
    with TestClient(create_app(db_path)) as client:
        user = post(client, '/api/register', {'name': '持久化用户', 'pwd': 'persistent-password'})
        post(client, '/api/createRoom', {
            'name': '重启后保留', 'owner': user['uid'], 'ownerName': user['name']
        })

    assert (tmp_path / 'web-game.json').is_file()
    with TestClient(create_app(db_path)) as client:
        assert post(client, '/api/login', {
            'name': '持久化用户', 'pwd': 'persistent-password'
        })['uid'] == user['uid']
        rooms = client.get('/game/api/rooms', params={'uid': user['uid']}).json()['rooms']
        assert [room['name'] for room in rooms] == ['重启后保留']
