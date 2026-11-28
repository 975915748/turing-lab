const assert = require('node:assert/strict');
const fs = require('node:fs');
const source = fs.readFileSync(require('node:path').join(__dirname, '../static/app.js'), 'utf8');

function section(start, end) {
  const from = source.indexOf(start);
  const to = source.indexOf(end, from);
  assert(from >= 0 && to > from, 'missing source section: ' + start);
  return source.slice(from, to);
}

assert.match(source, /function connectAccountSocket\(\)/);
assert.match(section('async function init(){', 'function renderAuth(){'), /connectAccountSocket\(\)/);
assert.doesNotMatch(source, /connectDmWs|stopDmWs|state\.dmSocket|state\.dmReconnect/);

const dmClear = section('function clearDmState(){', 'async function loadSocial(){');
assert.doesNotMatch(dmClear, /stopAccountSocket/);
const lobby = section('function renderLobby(){', 'async function loadAccounts(){');
assert.match(lobby, /loadRoomInvites\(\)/);
assert.match(lobby, /loadPublicRooms/);
assert.doesNotMatch(lobby, /stopAccountSocket/);

const inviteRefresh = section('async function loadRoomInvites(){', 'function currentAccountSocket');
assert.match(inviteRefresh, /personalModelEpoch/);
assert.match(inviteRefresh, /roomInviteRequestVersion/);
assert.doesNotMatch(inviteRefresh, /renderRoom\(|renderPersonalCenter\(/);

const socket = section('function connectAccountSocket(){', 'function capturePersonalCenterContext(){');
const dmHandler = section("if(message.type==='dm_message'){", "if(message.type==='room_invite_created'");
assert.match(socket, /state\.accountSocket/);
assert.match(socket, /setTimeout/);
assert.match(dmHandler, /location\.hash\.startsWith\('#profile'\)&&state\.dmUid===otherUid/);
assert.match(dmHandler, /markDmRead/);
assert.doesNotMatch(dmHandler, /renderPersonalCenter/);
assert.match(dmHandler, /await loadSocial\(\)/);

assert.match(source, /data-action="room-invite-accept"/);
assert.match(source, /data-action="public-room-join"/);
assert.match(source, /data-form="room-invite-send"/);
assert.match(source, /function renderQuestionBankPanel\(\)/);
assert.match(source, /#questionBankSearch/);
assert.match(source, /question-bank-random/);
assert.match(source, /dispatchEvent\(new Event\('input'/);

console.log('Community feature UI contracts passed.');
