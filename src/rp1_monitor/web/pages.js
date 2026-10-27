/* Independent, bookmarkable robot pages. APIs remain the source of device access. */
(() => {
  const labels = {fleet:'设备总览', overview:'整机状态', battery:'电池', joints:'关节状态', host:'主机运行负载', temperature:'关节温度', history:'历史曲线', processes:'核心进程', storage:'数据告警', diagnostics:'诊断助手'};
  const el = id => document.getElementById(id);
  const panels = [...document.querySelectorAll('[data-page-panel]')];
  const links = [...document.querySelectorAll('a[data-page]')];
  const base = new URL(document.baseURI);
  let items = [], selection = null, available = false, loaded = false, switchSignature = '', diagnosticRobot = null;
  function parse() {
    const prefix = base.pathname.endsWith('/') ? base.pathname : base.pathname + '/';
    const path = location.pathname.startsWith(prefix) ? location.pathname.slice(prefix.length).replace(/\/$/, '') : '!';
    if (!path || path === 'robots') return {page:'fleet', id:null};
    const parts = path.split('/');
    try {
      if (parts.length === 3 && parts[0] === 'robots' && parts[2] !== 'fleet' && labels[parts[2]]) return {page:parts[2], id:decodeURIComponent(parts[1])};
    } catch (_) { /* Malformed URLs have no valid device context. */ }
    return {page:null, id:null};
  }
  let route = parse();
  function url(page, id = route.id || selection) {
    return new URL(page === 'fleet' || !id ? 'robots' : `robots/${encodeURIComponent(id)}/${page}`, base).href;
  }
  function isActive(page) {
    return route.page === page && (page === 'fleet' || items.some(item => item.robot_id === route.id));
  }
  function paint() {
    const robot = items.find(item => item.robot_id === (route.id || selection));
    const valid = route.page && (route.page === 'fleet' || robot?.robot_id === route.id);
    const waiting = !!route.page && route.page !== 'fleet' && !valid && (!loaded || !available);
    panels.forEach(panel => { panel.hidden = !valid || panel.dataset.pagePanel !== route.page; });
    el('page-not-found').hidden = !!valid || waiting;
    el('page-loading').hidden = !waiting && (loaded || route.page !== 'fleet');
    el('page-loading').textContent = !available && loaded ? '平台连接中断，等待恢复后核对当前机器人。' : '正在读取设备状态…';
    el('page-title').textContent = labels[route.page] || '页面不存在';
    document.title = `${labels[route.page] || '页面不存在'}${route.id ? ' · ' + route.id : ''} · RP1 整机监控`;
    el('workspace-device-id').textContent = robot?.robot_id || route.id || '尚未登记机器人';
    el('workspace-source').textContent = robot ? robot.source_kind === 'simulated' ? '模拟数据 · 非实机' : 'ROS2来源 · 待实机验收' : '来源待确认';
    const dof = robot?.health?.motor_map?.dof ?? robot?.monitoring_configuration?.configuration?.dof;
    el('workspace-profile').textContent = dof ? `${dof} 自由度` : '机型以设备档案为准';
    el('workspace-status').textContent = !available ? '实时状态未知' : !robot ? '请选择已登记机器人' : !robot.enabled ? '已停用' : robot.online ? robot.data_fresh ? '采集在线 · 数据新鲜' : window.rp1RobotView?.hasCurrentObservations(robot) ? '采集在线 · 部分数据可用' : '采集在线 · 数据过期' : '离线 / 未连接';
    const signature = JSON.stringify(items.map(item => [item.robot_id, item.name]));
    if (signature !== switchSignature) {
      switchSignature = signature;
      const options = items.map(item => new Option(`${item.name} · ${item.robot_id}`, item.robot_id));
      el('robot-switch').replaceChildren(new Option(items.length ? '选择机器人' : '暂无登记设备', ''), ...options);
    }
    el('robot-switch').value = robot?.robot_id || '';
    links.forEach(link => {
      const page = link.dataset.page, disabled = page !== 'fleet' && !robot;
      link.href = url(page, robot?.robot_id);
      link.setAttribute('aria-disabled', String(disabled));
      if (page === route.page && valid) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
    });
    el('nav-diagnose').disabled = !robot;
    if (valid && route.page === 'diagnostics' && selection === route.id) {
      if (diagnosticRobot !== route.id && window.rp1Operations?.openInline) {
        diagnosticRobot = route.id;
        window.rp1Operations.openInline();
      }
    } else {
      diagnosticRobot = null;
      if (el('ops-dialog').open) el('ops-dialog').close();
    }
  }
  function enter(focus = true) {
    for (const id of ['thermal-dialog', 'ops-model-dialog']) if (el(id)?.open) el(id).close();
    paint();
    if (route.id && route.id !== selection) window.rp1SelectRobot?.(route.id);
    if (focus) { el('page-title').setAttribute('tabindex', '-1'); el('page-title').focus({preventScroll:true}); window.scrollTo({top:0, behavior:'instant'}); }
    document.dispatchEvent(new CustomEvent('rp1:page', {detail:{page:route.page, robotId:route.id}}));
    window.rp1RefreshView?.(route.page);
    window.rp1Showcase?.updateFleet?.(selection);
  }
  function navigate(page, id = route.id || selection) {
    if (!labels[page] || (page !== 'fleet' && !id)) return;
    const target = url(page, id);
    if (target === location.href) return;
    history.pushState({}, '', target);
    route = {page, id:page === 'fleet' ? null : id};
    enter();
  }
  window.rp1Pages = {navigate, url, robotId:() => route.id, currentPage:() => route.page, isActive,
    update(robots, selected, options = {}) {
      items = robots; selection = selected; available = !options.unavailable;
      if (available) loaded = true;
      paint();
    }};
  links.forEach(link => link.addEventListener('click', event => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (link.getAttribute('aria-disabled') !== 'true') navigate(link.dataset.page);
  }));
  el('robot-switch').addEventListener('change', event => { if (event.target.value) navigate(route.page && route.page !== 'fleet' ? route.page : 'overview', event.target.value); });
  window.addEventListener('popstate', () => { route = parse(); enter(); });
  document.addEventListener('DOMContentLoaded', paint);
  paint();
})();
