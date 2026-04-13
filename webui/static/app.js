let API = '';
let TOKEN = '';
let currentUser = '';
let currentTopicId = '';

// ─── API helper ───

async function api(method, path, body = null) {
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json' },
  };
  if (TOKEN) opts.headers['Authorization'] = `Bearer ${TOKEN}`;
  if (body) opts.body = JSON.stringify(body);
  const resp = await fetch(API + path, opts);
  if (resp.status === 401) { logout(); throw new Error('未认证'); }
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.detail || resp.statusText);
  return data;
}

// ─── Auth ───

async function login() {
  const pw = document.getElementById('password-input').value;
  try {
    const data = await api('POST', '/api/login', { password: pw });
    TOKEN = data.token;
    document.getElementById('login-page').style.display = 'none';
    document.getElementById('main-page').style.display = 'flex';
    loadUsers();
  } catch (e) {
    document.getElementById('login-error').textContent = e.message;
  }
}

function logout() {
  TOKEN = '';
  document.getElementById('login-page').style.display = '';
  document.getElementById('main-page').style.display = 'none';
}

// ─── Users ───

async function loadUsers() {
  const data = await api('GET', '/api/users');
  const tbody = document.getElementById('users-tbody');
  const sidebar = document.getElementById('user-list');
  tbody.innerHTML = '';
  sidebar.innerHTML = '';

  data.forEach(u => {
    const short = u.umo.split('_').slice(0, 3).join('_');
    tbody.innerHTML += `<tr>
      <td class="umo-cell" title="${u.umo}">${u.umo}</td>
      <td>${u.topic_count}</td>
      <td>${u.round_count}</td>
      <td><button class="btn-primary" onclick="selectUser('${u.umo}')">查看</button></td>
    </tr>`;
    sidebar.innerHTML += `<div class="sidebar-user" onclick="selectUser('${u.umo}')">
      <div class="user-label">${short}</div>
      <div>${u.topic_count} 主题 / ${u.round_count} 轮</div>
    </div>`;
  });
}

async function selectUser(umo) {
  currentUser = umo;
  currentTopicId = '';
  document.getElementById('view-users').style.display = 'none';
  document.getElementById('view-topic').style.display = 'none';
  document.getElementById('view-user-home').style.display = '';
  document.getElementById('user-home-title').textContent = `用户: ${umo}`;
  await loadTopics(umo);
}

function showUsers() {
  currentUser = '';
  currentTopicId = '';
  document.getElementById('view-users').style.display = '';
  document.getElementById('view-topic').style.display = 'none';
  document.getElementById('view-user-home').style.display = 'none';
  loadUsers();
}

function backToUserHome() {
  currentTopicId = '';
  document.getElementById('view-topic').style.display = 'none';
  document.getElementById('view-user-home').style.display = '';
  loadTopics(currentUser);
}

// ─── Topics ───

async function loadTopics(umo) {
  try {
    const data = await api('GET', `/api/users/${umo}/topics`);
    // Rebuild sidebar for this user
    const sidebar = document.getElementById('user-list');
    sidebar.innerHTML = `<div class="sidebar-user" onclick="showUsers()">&larr; 所有用户</div>`;

    data.topics.forEach(t => {
      sidebar.innerHTML += `<div class="sidebar-user" onclick="selectTopic('${umo}','${t.id}')">
        <div class="user-label">${esc(t.name)}</div>
        <div>${t.fragment_count || 0} 片段</div>
      </div>`;
    });

    // Render management table in user home
    renderTopicManagement(umo, data.topics);
  } catch (e) {
    toast('加载主题列表失败: ' + e.message, 'error');
    renderTopicManagement(umo, []);
  }
}

async function selectTopic(umo, topicId) {
  currentUser = umo;
  currentTopicId = topicId;
  document.getElementById('view-users').style.display = 'none';
  document.getElementById('view-user-home').style.display = 'none';
  document.getElementById('view-topic').style.display = '';

  const data = await api('GET', `/api/users/${umo}/topics/${topicId}`);
  document.getElementById('topic-title').textContent = `主题: ${data.name}`;
  document.getElementById('core-editor').value = data.core_md || '';
  document.getElementById('experience-editor').value = data.experience_md || '';

  // Fragments
  const fragList = document.getElementById('fragments-list');
  if (data.fragments.length === 0) {
    fragList.innerHTML = '<p style="color:var(--text-dim)">暂无记忆片段。</p>';
  } else {
    fragList.innerHTML = data.fragments.map(f => `
      <div class="fragment-card" onclick="showFragment('${umo}','${topicId}','${f.id}')">
        <div class="frag-header">
          <span class="frag-id">${f.id}</span>
          <div class="frag-actions">
            <button class="frag-transfer" onclick="event.stopPropagation();showTransferModal('${umo}','${topicId}','${f.id}')">转移</button>
            <button class="frag-delete" onclick="event.stopPropagation();deleteFragment('${umo}','${topicId}','${f.id}')">删除</button>
          </div>
        </div>
        <div class="frag-summary">${esc(f.summary)}</div>
        <div class="frag-meta">${f.rounds ? f.rounds.length : 0} 轮 · ${f.created_at ? f.created_at.slice(0, 19) : ''} · ${esc((f.keywords || []).join(', '))}</div>
      </div>`).join('');
  }

  // Merge list
  const index = await api('GET', `/api/users/${umo}/topics`);
  const mergeList = document.getElementById('merge-topic-list');
  mergeList.innerHTML = index.topics
    .filter(t => t.id !== topicId)
    .map(t => `<div class="merge-topic-item">
      <input type="checkbox" value="${t.id}">
      <label>${esc(t.name)} (${t.fragment_count || 0} 片段)</label>
    </div>`).join('');

  switchTab('core');
}

// ─── Tabs ───

function switchTab(name) {
  document.querySelectorAll('.tab-content').forEach(el => el.style.display = 'none');
  document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
  document.getElementById(`tab-${name}`).style.display = '';
  const idx = ['core', 'experience', 'fragments', 'merge'].indexOf(name);
  if (idx >= 0) {
    document.querySelectorAll('.tab')[idx]?.classList.add('active');
  }
}

// ─── Core / Experience save ───

async function saveCore() {
  const content = document.getElementById('core-editor').value;
  await api('PUT', `/api/users/${currentUser}/topics/${currentTopicId}/core`, { content });
  toast('core.md 已保存', 'success');
}

async function saveExperience() {
  const content = document.getElementById('experience-editor').value;
  await api('PUT', `/api/users/${currentUser}/topics/${currentTopicId}/experience`, { content });
  toast('experience.md 已保存', 'success');
}

// ─── Fragment detail ───

async function showFragment(umo, topicId, fragId) {
  const f = await api('GET', `/api/users/${umo}/topics/${topicId}/fragments/${fragId}`);
  document.getElementById('fragment-modal-title').textContent = `片段 ${f.id}`;
  const body = document.getElementById('fragment-modal-body');
  body.innerHTML = `
    <div><strong>摘要:</strong> ${esc(f.summary)}</div>
    <div style="margin-top:8px"><strong>关键词:</strong> ${esc((f.keywords || []).join(', '))}</div>
    <div style="margin-top:4px"><strong>创建:</strong> ${f.created_at || ''} · <strong>更新:</strong> ${f.updated_at || ''}</div>
    ${(f.rounds || []).map((r, i) => `
      <div class="round-block">
        <div class="round-label">轮次 ${i + 1} · ${r.timestamp || ''}</div>
        <div class="role-user">用户:</div>
        <div class="msg-text">${esc(r.user_message)}</div>
        <div class="role-assistant" style="margin-top:8px">助手:</div>
        <div class="msg-text">${esc(r.assistant_response)}</div>
      </div>`).join('')}`;
  document.getElementById('fragment-modal').style.display = '';
}

function closeFragmentModal() {
  document.getElementById('fragment-modal').style.display = 'none';
}

async function deleteFragment(umo, topicId, fragId) {
  if (!confirm('确定删除该片段？')) return;
  await api('DELETE', `/api/users/${umo}/topics/${topicId}/fragments/${fragId}`);
  selectTopic(umo, topicId);
  toast('片段已删除', 'success');
}

// ─── Transfer fragment ───

let _transferUmo = '';
let _transferSourceTopicId = '';
let _transferFragId = '';

async function showTransferModal(umo, sourceTopicId, fragId) {
  _transferUmo = umo;
  _transferSourceTopicId = sourceTopicId;
  _transferFragId = fragId;

  // Load all topics for this user
  const data = await api('GET', `/api/users/${umo}/topics`);
  const topicList = document.getElementById('transfer-topic-list');
  topicList.innerHTML = data.topics
    .filter(t => t.id !== sourceTopicId)
    .map(t => `<div class="merge-topic-item">
      <input type="radio" name="transfer-target" value="${t.id}" data-name="${esc(t.name)}">
      <label>${esc(t.name)} (${t.fragment_count || 0} 片段)</label>
    </div>`).join('');

  document.getElementById('transfer-modal').style.display = '';
}

function closeTransferModal() {
  document.getElementById('transfer-modal').style.display = 'none';
  _transferUmo = '';
  _transferSourceTopicId = '';
  _transferFragId = '';
}

async function executeTransfer() {
  const selected = document.querySelector('input[name="transfer-target"]:checked');
  if (!selected) { toast('请选择目标主题', 'error'); return; }

  const targetTopicId = selected.value;
  const targetTopicName = selected.dataset.name;

  if (!confirm(`确定将片段转移到「${targetTopicName}」？`)) return;

  try {
    await api('POST', `/api/users/${_transferUmo}/transfer-fragment`, {
      source_topic_id: _transferSourceTopicId,
      target_topic_id: targetTopicId,
      fragment_id: _transferFragId,
    });
    closeTransferModal();
    toast('片段已转移', 'success');
    selectTopic(_transferUmo, _transferSourceTopicId);
  } catch (e) {
    toast('转移失败: ' + e.message, 'error');
  }
}

// ─── Delete topic ───

async function deleteTopic() {
  if (!confirm('确定删除该主题及所有记忆？此操作不可恢复。')) return;
  await api('DELETE', `/api/users/${currentUser}/topics/${currentTopicId}`);
  currentTopicId = '';
  backToUserHome();
  toast('主题已删除', 'success');
}

// ─── Merge ───

async function executeMerge() {
  const checks = document.querySelectorAll('#merge-topic-list input[type=checkbox]:checked');
  if (checks.length === 0) { toast('请选择要合并的主题', 'error'); return; }
  const sourceIds = Array.from(checks).map(c => c.value);
  if (!confirm(`确定将 ${sourceIds.length} 个主题合并到当前主题？`)) return;
  const result = await api('POST', `/api/users/${currentUser}/merge-topics`, {
    source_ids: sourceIds,
    target_id: currentTopicId,
  });
  toast(`合并完成，迁移了 ${result.merged_fragments} 个片段`, 'success');
  selectTopic(currentUser, currentTopicId);
}

// ─── Dream ───

function showDreamModal() {
  document.getElementById('dream-instruction').value = '';
  document.getElementById('dream-modal').style.display = '';
}

function closeDreamModal() {
  document.getElementById('dream-modal').style.display = 'none';
}

async function triggerDream(withInstruction, singleTopic = false) {
  let instruction = '';
  if (withInstruction) {
    instruction = document.getElementById('dream-instruction').value.trim();
    closeDreamModal();
  }

  toast('Dream 开始执行...', 'success');

  try {
    const topicId = singleTopic ? currentTopicId : '';
    const result = await api('POST', `/api/users/${currentUser}/dream`, {
      topic_id: topicId,
      instruction,
    });
    const done = result.results.filter(r => r.status === 'done').length;
    const total = result.results.length;
    toast(`Dream 完成: ${done}/${total} 个主题整理成功`, 'success');
    if (currentTopicId) {
      selectTopic(currentUser, currentTopicId);
    }
    loadTopics(currentUser);
  } catch (e) {
    toast('Dream 执行失败: ' + e.message, 'error');
  }
}

// ─── Topic Management (user home) ───

function renderTopicManagement(umo, topics) {
  const tbody = document.getElementById('topic-mgmt-tbody');
  const emptyMsg = document.getElementById('topic-mgmt-empty');
  if (!topics || topics.length === 0) {
    tbody.innerHTML = '';
    emptyMsg.style.display = '';
    return;
  }
  emptyMsg.style.display = 'none';
  tbody.innerHTML = topics.map(t => {
    const overview = t.overview ? esc(t.overview) : '<span style="color:var(--text-dim)">-</span>';
    const created = t.created_at ? t.created_at.slice(0, 19).replace('T', ' ') : '-';
    return `<tr>
      <td><a href="javascript:void(0)" onclick="selectTopic('${umo}','${t.id}')" style="color:var(--accent);text-decoration:none">${esc(t.name)}</a></td>
      <td>${overview}</td>
      <td>${t.fragment_count || 0}</td>
      <td>${created}</td>
      <td class="topic-mgmt-actions">
        <button class="btn-sm btn-secondary" onclick="showRenameTopicModal('${t.id}','${esc(t.name).replace(/'/g, "\\'")}')">重命名</button>
        <button class="btn-sm btn-danger" onclick="deleteTopicFromHome('${t.id}','${esc(t.name).replace(/'/g, "\\'")}')">删除</button>
      </td>
    </tr>`;
  }).join('');
}

// ─── Create Topic ───

function showCreateTopicModal() {
  document.getElementById('create-topic-name').value = '';
  document.getElementById('create-topic-modal').style.display = '';
  document.getElementById('create-topic-name').focus();
}

function closeCreateTopicModal() {
  document.getElementById('create-topic-modal').style.display = 'none';
}

async function createTopic() {
  const name = document.getElementById('create-topic-name').value.trim();
  if (!name) { toast('请输入主题名称', 'error'); return; }
  try {
    await api('POST', `/api/users/${currentUser}/topics`, { name });
    closeCreateTopicModal();
    toast('主题已创建', 'success');
    loadTopics(currentUser);
  } catch (e) {
    toast('创建失败: ' + e.message, 'error');
  }
}

// ─── Rename Topic ───

let _renameTopicId = '';

function showRenameTopicModal(topicId, currentName) {
  _renameTopicId = topicId;
  const input = document.getElementById('rename-topic-name');
  input.value = currentName;
  document.getElementById('rename-topic-modal').style.display = '';
  input.focus();
  input.select();
}

function closeRenameTopicModal() {
  document.getElementById('rename-topic-modal').style.display = 'none';
  _renameTopicId = '';
}

async function renameTopic() {
  const newName = document.getElementById('rename-topic-name').value.trim();
  if (!newName) { toast('请输入新名称', 'error'); return; }
  try {
    await api('PUT', `/api/users/${currentUser}/topics/${_renameTopicId}/name`, { name: newName });
    closeRenameTopicModal();
    toast('主题已重命名', 'success');
    loadTopics(currentUser);
  } catch (e) {
    toast('重命名失败: ' + e.message, 'error');
  }
}

// ─── Delete Topic (from home) ───

async function deleteTopicFromHome(topicId, topicName) {
  if (!confirm(`确定删除主题「${topicName}」及所有记忆？此操作不可恢复。`)) return;
  try {
    await api('DELETE', `/api/users/${currentUser}/topics/${topicId}`);
    toast('主题已删除', 'success');
    loadTopics(currentUser);
  } catch (e) {
    toast('删除失败: ' + e.message, 'error');
  }
}

// ─── Toast ───

function toast(msg, type = 'success') {
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3000);
}

// ─── Util ───

function esc(s) {
  if (!s) return '';
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
