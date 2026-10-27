(() => {
  const el = id => document.getElementById(id);
  let control = '', fingerprint = '', busy = false;
  function message(text) { el('status').textContent = text; }
  function resetTrust() { fingerprint = ''; el('host-key').hidden = true; el('trust-host').checked = false; }
  function paint(state) {
    el('connection-badge').textContent = state.connected ? 'SSH 已连接' : '尚未连接';
    el('connected-actions').hidden = !state.connected;
    if (state.robot) el('open-robot').href = `/robots/${encodeURIComponent(state.robot.robot_id)}/overview`;
    if (state.connected) message(`已接入 ${state.robot?.robot_id || ''} · ${state.robot?.source_kind === 'simulated' ? '模拟数据，非实机' : 'ROS2 来源，仍待实机验收'}。后台正在接收，硬件缺失项保持未知。`);
  }
  async function request(path, body) {
    const response = await fetch(path, {method:body ? 'POST':'GET', credentials:'same-origin', cache:'no-store',
      headers:body ? {'Content-Type':'application/json','X-RP1-Station-Token':control}:{}, body:body ? JSON.stringify(body):undefined,
      signal:AbortSignal.timeout(45000)});
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '请求失败，请核对连接参数');
    return result;
  }
  async function refresh() {
    const state = await request('/api/v1/station'); control = state.control_token; paint(state);
    if (!state.connected) message('电脑后台已就绪。请填写地址与密码，连接机器人已有监听。');
  }
  el('ssh-target').addEventListener('input', resetTrust);
  el('ssh-port').addEventListener('input', resetTrust);
  el('connect-form').addEventListener('submit', async event => {
    event.preventDefault(); if (busy) return;
    busy = true; el('connect-button').disabled = true; message('正在建立SSH、核对采集端身份并启动电脑relay…');
    try {
      if (!control) await refresh();
      const result = await request('/api/v1/station/connect', {
        target:el('ssh-target').value.trim(), password:el('ssh-password').value,
        ssh_port:Number(el('ssh-port').value), collector_port:Number(el('collector-port').value),
        confirmed_fingerprint:el('trust-host').checked ? fingerprint : ''
      });
      if (result.status === 'host_key_required') {
        fingerprint = result.fingerprint; el('fingerprint').textContent = fingerprint; el('host-key').hidden = false;
        el('trust-host').checked = false; message('尚未发送密码。请核对主机指纹，勾选确认后再次连接。'); el('trust-host').focus();
      } else { el('ssh-password').value = ''; resetTrust(); paint(result); el('open-robot').focus(); }
    } catch(error) { el('ssh-password').value = ''; message(error.name === 'TimeoutError' ? '连接超时，请检查后台状态后重试。' : error.message); }
    finally { busy = false; el('connect-button').disabled = false; }
  });
  el('disconnect-button').addEventListener('click', async () => {
    if (busy) return; busy = true; el('disconnect-button').disabled = true;
    try { if (!control) await refresh(); paint(await request('/api/v1/station/disconnect',{})); message('电脑连接已断开，机器人监听继续运行，已保存的数据保留。'); }
    catch(error) { message(error.message); }
    finally { busy = false; el('disconnect-button').disabled = false; }
  });
  refresh().catch(()=>message('电脑后台未就绪，请确认使用 start-monitor.sh web 启动，并通过本机HTTP地址打开。'));
})();
