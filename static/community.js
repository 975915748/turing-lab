/* Community discovery UI: friend room invitations, public rooms and judge question prompts. */
(() => {
  const inviteState = {
    inviteVersion: 0,
    publicVersion: 0,
    inviteOpen: false,
    questionOpen: false,
    questionData: null,
    questionQuery: '',
    questionCategory: '',
    accountUid: null,
    accountCsrf: null,
    accountEpoch: null,
  };

  function formatInviteTime(value) {
    if (!value) return '';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
  }

  function lobbyVisible() {
    return !!(state.me?.user && app.querySelector('form[data-form="join"]') && !state.roomId &&
      !location.hash.startsWith('#profile'));
  }

  function currentAccount() {
    return {
      uid: state.me?.user?.uid || null,
      csrf: state.me?.user?.csrf || null,
      epoch: state.personalModelEpoch,
    };
  }

  function accountStillCurrent(context) {
    return !!context.uid && state.me?.user?.uid === context.uid &&
      state.me?.user?.csrf === context.csrf && state.personalModelEpoch === context.epoch;
  }

  async function loadRoomInvites() {
    const context = currentAccount();
    if (!context.uid) return null;
    const version = ++inviteState.inviteVersion;
    const result = await api('/api/me/room-invites');
    if (!accountStillCurrent(context) || inviteState.inviteVersion !== version) return state.roomInvites;
    state.roomInvites = result;
    renderInviteLobbyPanel();
    renderInviteRoomNotice();
    return result;
  }

  async function loadPublicRooms({ append = false } = {}) {
    const context = currentAccount();
    if (!context.uid) return [];
    const version = ++inviteState.publicVersion;
    const before = append ? state.communityPublicBeforeId : null;
    state.communityPublicLoading = true;
    renderPublicLobbyPanel();
    const params = new URLSearchParams({ limit: '30' });
    if (before) params.set('before_id', String(before));
    try {
      const result = await api('/api/public/rooms?' + params.toString());
      if (!accountStillCurrent(context) || inviteState.publicVersion !== version) return state.communityPublicRooms || [];
      const incoming = result.rooms || [];
      if (append) {
        const byId = new Map((state.communityPublicRooms || []).map(room => [room.room_id, room]));
        incoming.forEach(room => byId.set(room.room_id, room));
        state.communityPublicRooms = [...byId.values()];
      } else {
        state.communityPublicRooms = incoming;
      }
      state.communityPublicBeforeId = result.next_before_id || null;
      state.communityPublicHasMore = !!result.has_more;
      return state.communityPublicRooms;
    } finally {
      if (accountStillCurrent(context) && inviteState.publicVersion === version) {
        state.communityPublicLoading = false;
        renderPublicLobbyPanel();
      }
    }
  }

  function renderInviteLobbyPanel() {
    const target = document.getElementById('communityRoomInvites');
    if (!target) return;
    const received = state.roomInvites?.received || [];
    const sent = state.roomInvites?.sent || [];
    const pendingCount = Number(state.roomInvites?.pending_count ?? received.length);
    const truncated = !!state.roomInvites?.truncated;
    if (!received.length && !sent.length) {
      if (target.innerHTML) target.innerHTML = '';
      return;
    }
    const html = `
      <section class="card community-invites">
        <div class="label-line"><h2>好友房间邀请</h2><span class="pill">${pendingCount} 条待处理</span></div>
        ${truncated ? '<p class="hint">列表最多显示最近 100 条待处理邀请。</p>' : ''}
        ${received.length ? `
          <h3>收到的邀请</h3>
          <div class="community-list">
            ${received.map(item => `
              <article class="feed-item">
                <div class="label-line">
                  <strong>${esc(item.inviter_name)} 邀请你加入「${esc(item.room_title)}」</strong>
                  <span class="pill">${item.role === 'judge' ? '真人裁判' : '参赛者'}</span>
                </div>
                <p class="hint">有效期至 ${esc(formatInviteTime(item.expires_at))}</p>
                <div class="buttonrow">
                  <button class="small primary" type="button" data-community-action="invite-accept" data-id="${item.id}">接受并进入房间</button>
                  <button class="small" type="button" data-community-action="invite-reject" data-id="${item.id}">拒绝</button>
                </div>
              </article>`).join('')}
          </div>` : ''}
        ${sent.length ? `
          <h3 class="spaced">我发出的邀请</h3>
          <div class="community-list">
            ${sent.map(item => `
              <article class="feed-item">
                <div class="label-line">
                  <strong>${esc(item.invitee_name)}</strong>
                  <span class="pill">${item.role === 'judge' ? '真人裁判' : '参赛者'}</span>
                </div>
                <p class="hint">房间「${esc(item.room_title)}」 · 有效期至 ${esc(formatInviteTime(item.expires_at))}</p>
                <button class="small danger" type="button" data-community-action="invite-cancel" data-id="${item.id}">撤回邀请</button>
              </article>`).join('')}
          </div>` : ''}
      </section>`;
    if (target.innerHTML !== html) target.innerHTML = html;
  }

  function renderPublicLobbyPanel() {
    const target = document.getElementById('communityPublicRooms');
    if (!target) return;
    const rooms = state.communityPublicRooms || [];
    const cards = rooms.map(room => `
      <article class="card flat community-public-room">
        <div class="label-line">
          <div><div class="overline">ROOM ${room.room_id}</div><h3>${esc(room.title)}</h3></div>
          <span class="pill">${room.player_count}/${room.max_players} 位参赛者</span>
        </div>
        <p class="hint community-public-description">${esc(room.public_description || '房主尚未填写公开简介')}</p>
        <div class="tiny muted">房主：${esc(room.owner_display_name)} · 真人裁判 ${room.human_judge_count} 位 · AI 裁判 ${room.ai_judge_count} 位（共 ${room.human_judge_count + room.ai_judge_count}/${room.max_judges} 位裁判）· AI 参赛位 ${room.ai_player_count} 位</div>
        <div class="community-tags">${(room.tags || []).map(tag => `<span class="pill">${esc(tag)}</span>`).join('')}</div>
        <div class="buttonrow">
          ${room.can_join_player ? `<button class="small primary" type="button" data-community-action="public-join" data-id="${room.room_id}" data-role="player">以参赛者身份加入</button>` : ''}
          ${room.can_join_judge ? `<button class="small" type="button" data-community-action="public-join" data-id="${room.room_id}" data-role="judge">以真人裁判身份加入</button>` : ''}
        </div>
      </article>`).join('');
    const html = `
      <section class="card community-public-list">
        <div class="label-line">
          <div><h2>公开大厅</h2><p class="hint">只展示尚未开始、仍允许加入的公开房间。</p></div>
          <button class="small" type="button" data-community-action="public-refresh">刷新</button>
        </div>
        ${cards || (state.communityPublicLoading ? '<p class="muted">正在加载公开房间…</p>' : '<p class="muted">目前没有可加入的公开房间。</p>')}
        ${state.communityPublicHasMore ? '<button class="small spaced" type="button" data-community-action="public-more">加载更多</button>' : ''}
      </section>`;
    if (target.innerHTML !== html) target.innerHTML = html;
  }

  function renderInviteRoomNotice() {
    const count = state.roomInvites?.received?.length || 0;
    const existing = document.getElementById('communityRoomInviteNotice');
    if (!state.roomId || !count) {
      existing?.remove();
      return;
    }
    const signature = (state.roomInvites?.received || []).map(item => item.id).join(',');
    if (existing?.dataset.signature === signature) return;
    existing?.remove();
    const notice = document.createElement('section');
    notice.id = 'communityRoomInviteNotice';
    notice.dataset.count = String(count);
    notice.dataset.signature = signature;
    notice.className = 'notice community-room-invite-notice';
    notice.innerHTML = `
      <div><strong>你收到 ${count} 条好友房间邀请。</strong>
        <p class="hint">前往大厅查看将退出当前房间；如果你是房主，返回大厅会关闭房间。</p>
      </div>
      <button class="small" type="button" data-community-action="leave-for-invites">前往大厅查看（将退出当前房间）</button>`;
    app.prepend(notice);
  }

  function roomVisibilityEditable(room) {
    const first = room?.rounds?.find(round => round.number === 1);
    return !!(room?.can_manage && room.status === 'open' && !room.match_started &&
      first?.status === 'draft' && !first.question && !(first.questions || []).length);
  }

  function roomInvitesAllowed(room) {
    return roomVisibilityEditable(room) && !room.persona_frozen && !room.personas_frozen;
  }

  async function renderRoomManagementControls() {
    const room = state.room;
    const first = room?.rounds?.find(round => round.number === 1);
    const anchor = app.querySelector('form[data-form="round-timer"]')?.closest('.card') ||
      [...app.querySelectorAll('.card')].find(card => card.querySelector('h2')?.textContent.includes('参赛者管理'));
    if (!room?.can_manage || !anchor || !first) {
      document.getElementById('communityRoomControls')?.remove();
      return;
    }
    let panel = document.getElementById('communityRoomControls');
    if (!panel) {
      panel = document.createElement('section');
      panel.id = 'communityRoomControls';
      panel.className = 'community-room-controls';
      anchor.appendChild(panel);
    }
    const visibilityEditable = roomVisibilityEditable(room);
    const invitesAllowed = roomInvitesAllowed(room);
    const rosterFrozen = !!(room.persona_frozen || room.personas_frozen);
    const friends = state.social?.friends || [];
    const signature = JSON.stringify({
      roomId: state.roomId,
      matchNumber: room.match_number,
      publicListing: !!room.public_listing,
      publicDescription: room.public_description || '',
      visibilityEditable,
      invitesAllowed,
      rosterFrozen,
      inviteOpen: inviteState.inviteOpen,
      friends: friends.map(friend => [friend.uid, friend.note || '', friend.display_name || '']),
    });
    if (panel.dataset.signature === signature) return;
    const html = `
      <div class="divider"></div>
      ${visibilityEditable ? `
        <form data-community-form="room-visibility">
          <label class="check-row"><input type="checkbox" name="public_listing" ${room.public_listing ? 'checked' : ''} ${rosterFrozen && !room.public_listing ? 'disabled' : ''}> 在公开大厅展示</label>
          <label class="field">公开简介（最多 160 字）</label>
          <textarea name="public_description" maxlength="160" rows="2" placeholder="介绍比赛主题、参与要求或裁判安排">${esc(room.public_description || '')}</textarea>
          <div class="form-actions"><button class="small" type="submit">保存公开设置</button></div>
        </form>
        ${rosterFrozen ? `<p class="hint">名单已冻结，${room.public_listing ? '当前房间不会出现在公开大厅；你仍可关闭公开展示。若要重新公开，请先取消准备。' : '暂不能开启公开展示；取消准备后可以重新公开。'}</p>` : ''}
        <div class="divider"></div>
        ${invitesAllowed ? `
          <div class="label-line"><h3>邀请好友加入</h3><button class="small" type="button" data-community-action="invite-panel-toggle">${inviteState.inviteOpen ? '收起' : '展开'}</button></div>
          ${inviteState.inviteOpen ? (friends.length ? `
            <form data-community-form="room-invite">
              <label class="field">加入身份</label>
              <select name="role"><option value="player">参赛者</option><option value="judge">真人裁判</option></select>
              <div class="community-friend-list">
                ${friends.map(friend => {
                  const note = friend.note || friend.display_name;
                  const name = friend.display_name || note;
                  const label = note && name && note !== name
                    ? `${esc(note)}（${esc(name)}）`
                    : esc(name || note);
                  return `
                    <label class="check-row">
                      <input type="checkbox" name="uids" value="${esc(friend.uid)}">
                      <span>${label} · ${esc(friend.uid)}</span>
                    </label>`;
                }).join('')}
              </div>
              <div class="form-actions"><button class="primary" type="submit">发送所选邀请</button></div>
              <p class="hint">邀请有效期 30 分钟；只能邀请当前好友。相同身份的待处理邀请会复用。</p>
            </form>` : '<p class="hint">尚无好友。请先在个人中心添加好友。</p><button class="small" type="button" data-action="personal-center">前往个人中心添加好友</button>') : ''}
        ` : '<p class="hint">名单已冻结，暂不能发送新的好友邀请。</p>'}
      ` : (room.public_listing ? '<p class="hint">首轮已开始或问题已提交，公开大厅展示已停止；本场不能重新公开。</p>' : '')}
    `;
    panel.innerHTML = html;
    panel.dataset.signature = signature;
  }

  function ensureLobbyPanels() {
    if (!lobbyVisible()) return;
    const grid = app.querySelector('.grid2');
    if (!grid) return;
    let panels = document.getElementById('communityLobbyPanels');
    if (!panels) {
      panels = document.createElement('section');
      panels.id = 'communityLobbyPanels';
      panels.className = 'spaced';
      panels.innerHTML = '<div id="communityRoomInvites"></div><div id="communityPublicRooms"></div>';
      grid.after(panels);
    }
    renderInviteLobbyPanel();
    renderPublicLobbyPanel();

    const roomForm = app.querySelector('form[data-form="room"]');
    if (roomForm && !roomForm.dataset.communityEnhanced) {
      roomForm.dataset.communityEnhanced = 'true';
      const actions = roomForm.querySelector('.form-actions');
      const fields = document.createElement('div');
      fields.className = 'community-create-visibility';
      fields.innerHTML = `
        <label class="check-row"><input type="checkbox" name="public_listing"> 在公开大厅展示（默认关闭）</label>
        <label class="field">公开简介（最多 160 字）</label>
        <textarea name="public_description" maxlength="160" rows="2" placeholder="介绍比赛主题、参与要求或裁判安排"></textarea>`;
      if (actions) actions.before(fields);
      else roomForm.appendChild(fields);
    }
  }

  function filteredQuestions() {
    const questions = inviteState.questionData?.questions || [];
    const query = inviteState.questionQuery.trim().toLocaleLowerCase();
    return questions.filter(item => (!inviteState.questionCategory || item.category === inviteState.questionCategory) &&
      (!query || [item.title, item.text, ...(item.tags || [])].join(' ').toLocaleLowerCase().includes(query)));
  }

  function renderQuestionResults() {
    const target = document.getElementById('communityQuestionResults');
    if (!target) return;
    const rows = filteredQuestions();
    const categories = inviteState.questionData?.categories || [];
    const html = rows.length ? rows.map(item => {
      const category = categories.find(entry => entry.id === item.category)?.name || item.category;
      return `
      <article class="feed-item community-question-item">
        <div class="label-line"><div><strong>${esc(item.title)}</strong><div class="tiny muted">${esc(category)}</div></div><button class="small" type="button" data-community-action="question-fill" data-id="${esc(item.id)}">填入问题</button></div>
        <p class="community-question-text">${esc(item.text)}</p>
        <div class="community-tags">${(item.tags || []).map(tag => `<span class="pill">${esc(tag)}</span>`).join('')}</div>
      </article>`;
    }).join('') : '<p class="muted">没有匹配的题目，可以更换分类或搜索词。</p>';
    if (target.innerHTML !== html) target.innerHTML = html;
  }

  function renderQuestionBank(form) {
    const textarea = form.querySelector('textarea[name="text"]');
    if (!textarea) return;
    let panel = document.getElementById('communityQuestionBank');
    if (!panel) {
      panel = document.createElement('div');
      panel.id = 'communityQuestionBank';
      textarea.before(panel);
    }
    if (!inviteState.questionOpen) {
      const html = '<button class="small" type="button" data-community-action="question-open">出题灵感库</button>';
      if (panel.dataset.open !== 'false' || panel.innerHTML !== html) {
        panel.innerHTML = html;
        panel.dataset.open = 'false';
      }
      return;
    }
    const categories = inviteState.questionData?.categories || [];
    const html = `
      <div class="community-question-bank">
        <div class="label-line"><strong>出题灵感库</strong><button class="small" type="button" data-community-action="question-close">收起</button></div>
        <div class="community-question-filters">
          <select data-community-question-category>
            <option value="">全部分类</option>
            ${categories.map(item => `<option value="${esc(item.id)}" ${inviteState.questionCategory === item.id ? 'selected' : ''}>${esc(item.name)}</option>`).join('')}
          </select>
          <input type="search" data-community-question-search maxlength="80" placeholder="搜索题目、内容或标签" value="${esc(inviteState.questionQuery)}">
        </div>
        <div class="buttonrow"><button class="small" type="button" data-community-action="question-random">随机抽取一题</button></div>
        <div id="communityQuestionResults"></div>
      </div>`;
    if (panel.dataset.open !== 'true') {
      panel.innerHTML = html;
      panel.dataset.open = 'true';
    }
    renderQuestionResults();
  }

  async function openQuestionBank() {
    inviteState.questionOpen = true;
    const form = app.querySelector('form[data-form="judge-question"]');
    if (!form) return;
    renderQuestionBank(form);
    if (!inviteState.questionData) {
      const account = currentAccount();
      const context = { ...account, roomId: state.roomId, matchNumber: state.room?.match_number,
        roomEpoch: state.roomContextEpoch };
      try {
        const data = await api('/api/question-bank?limit=100');
        if (!accountStillCurrent(context) || state.roomId !== context.roomId ||
            state.room?.match_number !== context.matchNumber || state.roomContextEpoch !== context.roomEpoch) return;
        inviteState.questionData = data;
        const currentPanel = document.getElementById('communityQuestionBank');
        if (currentPanel) currentPanel.dataset.open = 'false';
        renderQuestionBank(app.querySelector('form[data-form="judge-question"]') || form);
      } catch (error) {
        if (accountStillCurrent(context) && state.roomId === context.roomId &&
            state.roomContextEpoch === context.roomEpoch) toast(error.message || '题库加载失败');
      }
    }
  }

  function fillQuestion(id) {
    const item = (inviteState.questionData?.questions || []).find(question => question.id === id);
    const textarea = app.querySelector('form[data-form="judge-question"] textarea[name="text"]');
    if (!item || !textarea) return;
    if (textarea.value.trim() && !confirm('当前问题框已有内容。确定用题库内容替换吗？')) return;
    textarea.value = item.text;
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    textarea.focus();
    toast('题目已填入；仍需由你检查、编辑并主动提交。');
  }

  function ensureCommunityUi() {
    const uid = state.me?.user?.uid || null;
    if (inviteState.roomId !== (state.roomId || null)) {
      inviteState.roomId = state.roomId || null;
      inviteState.inviteOpen = false;
      inviteState.questionOpen = false;
    }
    const csrf = state.me?.user?.csrf || null;
    const epoch = state.personalModelEpoch;
    if (inviteState.accountUid !== uid || inviteState.accountCsrf !== csrf || inviteState.accountEpoch !== epoch) {
      inviteState.accountUid = uid;
      inviteState.accountCsrf = csrf;
      inviteState.accountEpoch = epoch;
      inviteState.inviteOpen = false;
      inviteState.questionOpen = false;
      inviteState.questionData = null;
      inviteState.questionQuery = '';
      inviteState.questionCategory = '';
      if (uid) {
        loadRoomInvites().catch(() => {});
        loadPublicRooms().catch(() => {});
      }
    }
    if (!uid) return;
    ensureLobbyPanels();
    renderRoomManagementControls();
    const judgeForm = app.querySelector('form[data-form="judge-question"]');
    if (judgeForm) renderQuestionBank(judgeForm);
    renderInviteRoomNotice();
  }

  document.addEventListener('click', async event => {
    const button = event.target.closest('[data-community-action]');
    if (!button) return;
    const action = button.dataset.communityAction;
    const account = currentAccount();
    if (!account.uid) return;
    const roomIdAtClick = state.roomId;
    const roomEpochAtClick = state.roomContextEpoch;
    event.preventDefault();
    event.stopImmediatePropagation();
    try {
      if (action === 'invite-accept') {
        const result = await api('/api/room-invites/' + Number(button.dataset.id) + '/accept', { method: 'POST' });
        if (!accountStillCurrent(account) || state.roomId !== roomIdAtClick ||
            state.roomContextEpoch !== roomEpochAtClick) return;
        await loadRoomInvites();
        if (!accountStillCurrent(account) || state.roomId !== roomIdAtClick ||
            state.roomContextEpoch !== roomEpochAtClick) return;
        await openRoom(result.room_id);
        toast('已接受邀请并进入房间');
      } else if (action === 'invite-reject' || action === 'invite-cancel') {
        const endpoint = action === 'invite-reject' ? 'reject' : 'cancel';
        await api('/api/room-invites/' + Number(button.dataset.id) + '/' + endpoint, { method: 'POST' });
        if (!accountStillCurrent(account)) return;
        await loadRoomInvites();
        toast(action === 'invite-reject' ? '已拒绝邀请' : '已撤回邀请');
      } else if (action === 'public-join') {
        const result = await api('/api/rooms/' + Number(button.dataset.id) + '/join', {
          method: 'POST', body: { role: button.dataset.role },
        });
        if (!accountStillCurrent(account) || state.roomId !== roomIdAtClick ||
            state.roomContextEpoch !== roomEpochAtClick) return;
        await openRoom(result.room_id);
        toast(result.role === 'judge' ? '已作为真人裁判加入公开房间' : '已作为参赛者加入公开房间');
      } else if (action === 'public-refresh') {
        await loadPublicRooms();
      } else if (action === 'public-more') {
        await loadPublicRooms({ append: true });
      } else if (action === 'leave-for-invites') {
        await leaveCurrentRoom();
      } else if (action === 'invite-panel-toggle') {
        inviteState.inviteOpen = !inviteState.inviteOpen;
        if (inviteState.inviteOpen && !state.social) await loadSocial();
        if (!accountStillCurrent(account) || state.roomId !== roomIdAtClick ||
            state.roomContextEpoch !== roomEpochAtClick) return;
        await renderRoomManagementControls();
      } else if (action === 'question-open') {
        await openQuestionBank();
      } else if (action === 'question-close') {
        inviteState.questionOpen = false;
        const form = app.querySelector('form[data-form="judge-question"]');
        if (form) renderQuestionBank(form);
      } else if (action === 'question-fill') {
        fillQuestion(button.dataset.id);
      } else if (action === 'question-random') {
        const rows = filteredQuestions();
        if (rows.length) fillQuestion(rows[Math.floor(Math.random() * rows.length)].id);
      }
    } catch (error) {
      if (accountStillCurrent(account) &&
          ['invite-accept', 'invite-reject', 'invite-cancel'].includes(action)) {
        await loadRoomInvites().catch(() => {});
      }
      toast(error.message || '操作失败');
    }
  }, true);

  document.addEventListener('submit', async event => {
    const form = event.target.closest('form[data-form],form[data-community-form]');
    if (!form) return;
    const type = form.dataset.communityForm || form.dataset.form;
    if (!['room', 'room-invite', 'room-visibility'].includes(type)) return;
    const account = currentAccount();
    if (!account.uid) return;
    const roomIdAtSubmit = state.roomId;
    const matchAtSubmit = state.room?.match_number;
    const roomEpochAtSubmit = state.roomContextEpoch;
    event.preventDefault();
    event.stopImmediatePropagation();
    const submit = form.querySelector('button[type="submit"]');
    if (submit) submit.disabled = true;
    try {
      const values = Object.fromEntries(new FormData(form));
      if (type === 'room') {
        const result = await api('/api/rooms', {
          method: 'POST',
          body: {
            title: values.title,
            judge: values.judge || '裁判',
            public_listing: !!form.elements.public_listing?.checked,
            public_description: values.public_description || '',
          },
        });
        if (!accountStillCurrent(account) || state.roomId !== roomIdAtSubmit ||
            state.roomContextEpoch !== roomEpochAtSubmit) return;
        await openRoom(result.room_id);
        toast('房间创建成功，可以分享邀请码');
      } else if (type === 'room-invite') {
        const uids = [...form.querySelectorAll('input[name="uids"]:checked')].map(input => input.value);
        if (!uids.length) throw new Error('请至少选择一位好友。');
        if (roomIdAtSubmit === null) throw new Error('当前房间已变化，请刷新后重试。');
        const result = await api('/api/rooms/' + roomIdAtSubmit + '/invites', {
          method: 'POST', body: { uids, role: values.role },
        });
        if (!accountStillCurrent(account) || state.roomId !== roomIdAtSubmit ||
            state.room?.match_number !== matchAtSubmit || state.roomContextEpoch !== roomEpochAtSubmit) return;
        inviteState.inviteOpen = false;
        await loadRoomInvites();
        if (!accountStillCurrent(account) || state.roomId !== roomIdAtSubmit ||
            state.room?.match_number !== matchAtSubmit || state.roomContextEpoch !== roomEpochAtSubmit) return;
        await refreshRoom();
        const sent = (result.results || []).filter(item => item.status === 'sent').length;
        const skipped = (result.results || []).filter(item => item.status === 'skipped').length;
        toast('已发送 ' + sent + ' 条邀请' + (skipped ? '，跳过 ' + skipped + ' 位' : ''));
      } else if (type === 'room-visibility') {
        const public_listing = !!form.elements.public_listing?.checked;
        if (roomIdAtSubmit === null) throw new Error('当前房间已变化，请刷新后重试。');
        await api('/api/rooms/' + roomIdAtSubmit + '/visibility', {
          method: 'PUT',
          body: { public_listing, public_description: values.public_description || '' },
        });
        if (!accountStillCurrent(account) || state.roomId !== roomIdAtSubmit ||
            state.room?.match_number !== matchAtSubmit || state.roomContextEpoch !== roomEpochAtSubmit) return;
        await refreshRoom();
        if (!accountStillCurrent(account) || state.roomId !== roomIdAtSubmit ||
            state.room?.match_number !== matchAtSubmit || state.roomContextEpoch !== roomEpochAtSubmit) return;
        await loadPublicRooms();
        toast(public_listing ? '房间已加入公开大厅' : '已关闭公开大厅展示');
      }
    } catch (error) {
      toast(error.message || '操作失败');
    } finally {
      if (submit) submit.disabled = false;
    }
  }, true);

  document.addEventListener('input', event => {
    if (event.target.matches('[data-community-question-search]')) {
      inviteState.questionQuery = event.target.value;
      renderQuestionResults();
    }
  });
  document.addEventListener('change', event => {
    if (event.target.matches('[data-community-question-category]')) {
      inviteState.questionCategory = event.target.value;
      renderQuestionResults();
    }
  });

  window.loadRoomInvites = loadRoomInvites;
  window.loadPublicRooms = loadPublicRooms;

  const observer = new MutationObserver(() => ensureCommunityUi());
  observer.observe(app, { childList: true, subtree: true });
  window.addEventListener('focus', () => {
    if (lobbyVisible()) {
      loadRoomInvites().catch(() => {});
      loadPublicRooms().catch(() => {});
    }
  });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && lobbyVisible()) {
      loadRoomInvites().catch(() => {});
      loadPublicRooms().catch(() => {});
    }
  });

  ensureCommunityUi();
})();
