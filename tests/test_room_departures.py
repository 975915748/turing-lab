"""Room departures use real server membership, not browser navigation state."""
import concurrent.futures
import threading
import pytest
from fastapi.testclient import TestClient
from app import db_open
from test_social_points import fixture_clients, post, request
from test_model_operations import operation_lab

@pytest.fixture
def lab(tmp_path):
    path,app,admin,host,player,third,judge=fixture_clients(tmp_path)
    room=post(host,"/api/rooms",{"title":"Departures"})
    base=f"/api/rooms/{room['room_id']}"
    for c in (host,player,third): post(c,"/api/join",{"code":room["code"]})
    return path,app,admin,host,player,third,judge,room,base

def leave(c,base,match=1):
    return post(c,base+"/leave",{"match_number":match,"confirm":True})

def test_player_leaves_unlocks_and_cannot_rejoin_started_match(lab):
    path,_,_,host,player,third,_,room,base=lab
    post(host,base+"/question",{"text":"Question"},match=1)
    assert player.get("/api/me").json()["user"]["name_change_locked"]
    leave(player,base)
    assert not player.get("/api/me").json()["user"]["name_change_locked"]
    request(player,"PUT","/api/me/profile",{"display_name":"new name"})
    post(player,"/api/join",{"code":room["code"]},status=403)
    assert player.get(base).status_code==403
    leave(player,base)  # idempotent retry
    assert host.get(base).json()["status"]=="open"

def test_prestart_leave_can_rejoin_but_kicked_user_cannot(lab):
    _,_,_,host,player,third,_,room,base=lab
    alias=player.get(base).json()["mine"]
    leave(player,base)
    assert post(player,"/api/join",{"code":room["code"]})["alias"]==alias
    uid=third.get("/api/me").json()["user"]["id"]
    post(host,f"{base}/members/{uid}/remove",match=1)
    post(third,"/api/join",{"code":room["code"]},status=403)

def test_removed_after_start_can_rename(lab):
    _,_,_,host,player,_,_,_,base=lab
    post(host,base+"/question",{"text":"Question"},match=1)
    uid=player.get("/api/me").json()["user"]["id"]
    post(host,f"{base}/members/{uid}/remove",match=1)
    request(player,"PUT","/api/me/profile",{"display_name":"removed and free"})

def test_owner_leaves_closes_preserves_records_and_never_settles(lab):
    path,_,_,host,player,_,_,room,base=lab
    post(host,base+"/question",{"text":"Preserved question"},match=1)
    post(player,base+"/answers",{"text":"Preserved answer"},match=1)
    post(player,base+"/chat",{"match_number":1,"text":"Preserved chat"},match=1)
    result=leave(host,base)
    assert result["closed"]
    assert host.get(base).json()["status"]=="closed"
    with db_open(path) as db:
        assert db.execute("SELECT COUNT(*) FROM answers").fetchone()[0]==1
        assert db.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0]==1
        assert db.execute("SELECT COUNT(*) FROM match_settlements").fetchone()[0]==0
    for c in (host,player):
        assert not c.get("/api/me").json()["user"]["name_change_locked"]
    post(player,base+"/answers",{"text":"Late answer"},status=409,match=1)
    post(player,"/api/join",{"code":room["code"]},status=409)
    assert host.get(base+"/log").status_code==200
    leave(host,base)
    post(host,base+"/restart",{"match_number":1,"confirm":True})
    post(player,base+"/leave",{"match_number":1,"confirm":True},status=409)
    assert host.get(base).json()["match_number"]==2

def test_owner_withdraw_only_retains_management(lab):
    _,_,_,host,player,_,_,_,base=lab
    post(host,base+"/question",{"text":"Q"},match=1)
    post(host,base+"/withdraw",{"match_number":1,"confirm":True})
    data=host.get(base).json()
    assert data["status"]=="open" and data["can_manage"] and data["mine"] is None
    post(host,base+"/withdraw",{"match_number":1,"confirm":True})
    assert len(data["humans"])==2

def test_judge_can_leave_unlock_and_judge_elsewhere(lab):
    _,_,_,host,player,_,judge,room,base=lab
    post(judge,"/api/join",{"code":room["code"],"role":"judge"})
    post(judge,base+"/judge-questions",{"text":"Judge Q"},match=1)
    post(host,base+"/start",match=1)
    leave(judge,base)
    assert not judge.get("/api/me").json()["user"]["name_change_locked"]
    post(judge,"/api/join",{"code":room["code"],"role":"judge"},status=403)
    post(judge,base+"/judge-questions",{"text":"Late judge Q"},status=403,match=1)
    other=post(player,"/api/rooms",{"title":"other"})
    post(judge,"/api/join",{"code":other["code"],"role":"judge"})
    assert host.get(base).json()["judges"]==[]

def test_leave_has_csrf_membership_and_match_guards(lab):
    _,_,admin,host,player,_,_,_,base=lab
    assert player.post(base+"/leave",json={"match_number":1,"confirm":True}).status_code==403
    post(player,base+"/leave",{"match_number":99,"confirm":True},status=409)
    post(admin,base+"/leave",{"match_number":1,"confirm":True})
    assert host.get(base).json()["status"]=="open"  # observer admin never closes another owner's room

def test_remaining_submissions_reveal_after_departure(lab):
    _,_,_,host,player,third,_,_,base=lab
    post(host,base+"/question",{"text":"Q"},match=1)
    post(host,base+"/answers",{"text":"H"},match=1)
    post(player,base+"/answers",{"text":"P"},match=1)
    leave(third,base)
    assert host.get(base).json()["rounds"][0]["status"]=="revealed"

def test_departed_player_is_excluded_from_points_and_old_answers_retained(lab):
    path,_,_,host,player,third,_,room,base=lab
    post(host,base+"/question",{"text":"Q"},match=1)
    for c in (host,player,third):post(c,base+"/answers",{"text":"answer"},match=1)
    leave(third,base)
    aliases=[h["alias"] for h in host.get(base).json()["humans"]]
    post(host,base+"/verdict",{"predictions":dict.fromkeys(aliases,"AI"),"evidence":"valid","confidence":80},match=1)
    post(host,base+"/reveal",match=1)
    assert third.get("/api/me/social").json()["points"]["total_points"]=="0"
    assert host.get("/api/me/social").json()["points"]["total_points"]=="10"
    with db_open(path) as db:
        assert db.execute("SELECT COUNT(*) FROM answers").fetchone()[0]==3
        assert db.execute("SELECT COUNT(*) FROM points_ledger").fetchone()[0]==2
    leave(host,base)
    assert host.get(base).json()["status"]=="finished"
    assert host.get("/api/me/social").json()["points"]["total_points"]=="10"

def test_exit_migration_preserves_existing_rows_and_is_repeatable(lab):
    from app import init_db
    path,_,_,host,player,_,_,room,base=lab
    with db_open(path) as db:
        db.execute("ALTER TABLE members DROP COLUMN leave_reason")
        db.execute("ALTER TABLE room_judges DROP COLUMN left_at")
        db.execute("ALTER TABLE rooms DROP COLUMN closed_at")
    init_db(path)
    init_db(path)
    assert host.get(base).json()["title"]=="Departures"
    assert len(host.get(base).json()["humans"])==3
    leave(player,base)

def test_leave_only_releases_one_rooms_lock(lab):
    _,_,_,host,player,third,_,room,base=lab
    other=post(third,"/api/rooms",{"title":"Other active match"})
    base2=f"/api/rooms/{other['room_id']}"
    for c in (third,player):post(c,"/api/join",{"code":other["code"]})
    post(host,base+"/question",{"text":"Q1"},match=1)
    post(third,base2+"/question",{"text":"Q2"},match=1)
    leave(player,base)
    assert player.get("/api/me").json()["user"]["name_change_locked"]
    leave(player,base2)
    assert not player.get("/api/me").json()["user"]["name_change_locked"]

def test_close_rejects_other_mutations_even_legacy_headerless_calls(lab):
    _,_,_,host,player,_,_,room,base=lab
    leave(host,base)
    post(host,base+"/ai",{"name":"late"},status=409)
    post(host,base+"/ai-judges",{"name":"late"},status=409)
    post(host,base+"/packets",status=409)
    post(host,base+"/reveal",status=409)
    post(host,base+"/chat",{"match_number":1,"text":"late"},status=409)

def test_judge_prestart_exit_rejoin_and_restart_after_exit(lab):
    _,_,_,host,player,_,judge,room,base=lab
    post(judge,"/api/join",{"code":room["code"],"role":"judge"})
    leave(judge,base)
    post(judge,"/api/join",{"code":room["code"],"role":"judge"})
    assert len(host.get(base).json()["judges"])==1
    leave(judge,base)
    leave(host,base)
    post(host,base+"/restart",{"match_number":1,"confirm":True})
    post(judge,"/api/join",{"code":room["code"],"role":"judge"})


def test_prestart_returning_member_cannot_overfill_room(lab):
    _,_,_,host,player,_,_,room,base=lab
    for i in range(5):post(host,base+'/ai',{'name':f'AI {i}'})
    leave(player,base)
    post(host,base+'/ai',{'name':'replacement AI'})
    post(player,'/api/join',{'code':room['code']},status=409)
    data=host.get(base).json()
    assert len(data['humans'])+len(data['ais'])==8


def test_departed_judge_cannot_change_role_in_roster_recovery(lab):
    _,_,admin,host,player,third,judge,room,base=lab
    post(judge,'/api/join',{'code':room['code'],'role':'judge'})
    post(judge,base+'/judge-questions',{'text':'First question'},match=1)
    post(host,base+'/start',match=1)
    for c in (host,player,third):post(c,base+'/answers',{'text':'Answer'},match=1)
    post(host,base+'/next',match=1)
    for c in (player,third):
        uid=c.get('/api/me').json()['user']['uid']
        request(admin,'DELETE','/api/admin/users/'+uid,{'confirm':True})
    assert host.get(base).json()['roster_recovery']
    leave(judge,base)
    post(judge,'/api/join',{'code':room['code'],'role':'player'},status=403)


def test_close_discards_inflight_model_result_but_preserves_attempt(operation_lab):
    lab=operation_lab
    owner,base=lab['admin'],lab['base']
    post(owner,base+'/ai-judges',{'name':'Late question','model_profile_id':lab['profile_id']})
    block={'started':threading.Event(),'release':threading.Event()}
    lab['state']['block']=block
    second=TestClient(lab['app'])
    post(second,'/api/login',{'username':'ops_admin','password':'ops-admin-password'})
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(post,second,base+'/ai-judges/questions/generate')
        try:
            assert block['started'].wait(5)
            leave(owner,base)
        finally:
            block['release'].set()
        assert future.result(timeout=15)['generated']==0
    room=owner.get(base).json()
    assert room['status']=='closed' and room['rounds'][0]['questions']==[]
    assert room['model_request_count']==1
    with db_open(lab['path']) as db:
        task=db.execute('SELECT * FROM model_operation_tasks').fetchone()
        assert task['state']=='stale' and not task['result_applied']
        assert not db.execute('SELECT 1 FROM match_settlements').fetchone()


