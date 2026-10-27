/* Separate read-only component pages. Statistics belong to the queried time range. */
(() => {
  const el = id => document.getElementById(id), kinds = ['battery','joints','host'];
  const charts = {battery:['battery-soc','battery-temperature','battery-voltage','battery-current'], joints:['joints-temperature','joints-mos','joints-torque','joints-rms'], host:['host-cpu','host-memory','host-temperature']};
  let robot = null, available = false, generation = 0, activeKey = '', inflight = null, result = null, lastRead = 0, jointSignature = '';
  const number = (value, unit, digits=1) => Number.isFinite(value) ? `${value.toFixed(digits)} ${unit}` : '—';
  const text = (id, value) => { if (el(id)) el(id).textContent = value; };
  const page = () => window.rp1Pages?.currentPage();
  const active = () => kinds.includes(page()) && window.rp1Pages.isActive(page());
  function duration(value) {
    if (!Number.isFinite(value) || value < 0) return '—';
    return `${Math.floor(value/86400)}天 ${String(Math.floor(value/3600)%24).padStart(2,'0')}:${String(Math.floor(value/60)%60).padStart(2,'0')}:${String(Math.floor(value)%60).padStart(2,'0')}`;
  }
  function clearStats() {
    for (const id of ['battery-page-current-peak','battery-page-voltage-range','joints-page-rms','joints-page-temperature-max','joints-page-torque-peak']) text(id,'—');
    text('battery-current-extrema','所选时段电流极值待读取');
    text('joints-rms-note','等待有效配对样本');
  }
  function clearCurves() {
    result = null; clearStats();
    for (const prefixes of Object.values(charts)) for (const prefix of prefixes) {
      el(prefix+'-chart').replaceChildren(); el(prefix+'-empty').classList.remove('hidden');
      text(prefix+'-resolution','等待查询当前设备与所选时段');
    }
  }
  function changed() {
    generation++; activeKey = ''; inflight = null; lastRead = 0; jointSignature = '';
    clearCurves();
    el('host-thermal-select').replaceChildren(new Option('等待热区数据',''));
    el('host-thermal-select').dataset.signature = '';
    for (const kind of kinds) { el(kind+'-refresh').disabled = false; text(kind+'-status','设备已切换，等待查询当前设备。'); }
  }
  function update(snapshot, online) {
    robot = snapshot || null; available = !!online;
    const thermalSelector = el('host-thermal-select');
    const thermalKeys = new Set([...(robot?.catalog || []).map(item=>item.key),...(robot?.health?.host?.sensors || []).map(sensor=>sensor.key)]);
    if (available && thermalSelector.value && !thermalKeys.has(thermalSelector.value)) {
      if (page() === 'host') selectionChanged();
      thermalSelector.replaceChildren(new Option('等待热区数据',''));
      thermalSelector.dataset.signature = '';
    }
    const timeVerified = robot?.transport?.freshness_verified === true;
    const realtimeAllowed = window.rp1RobotView?.realtimeAllowed(robot) ?? (robot?.transport ? timeVerified : !robot?.clock_unverified);
    const health = available && realtimeAllowed ? robot?.health : null, battery = health?.battery || {}, host = health?.host || {};
    text('battery-page-soc',number(battery.soc,'%')); text('battery-page-temp',number(battery.temperature,'°C'));
    text('battery-page-voltage',number(battery.voltage,'V',2)); text('battery-page-current',number(battery.current,'A',2));
    text('host-page-cpu',number(host.cpu_usage,'%')); text('host-page-memory',number(host.memory_usage,'%'));
    text('host-page-temperature',number(host.max_temperature,'°C')); text('host-page-uptime',duration(host.uptime_seconds));
    text('host-uptime-note',!Number.isFinite(host.uptime_seconds) ? '未提供有效开机时长' : robot?.source_kind === 'simulated' ? '模拟源本次启动时长 · 非实机' : '机器人主机 /proc/uptime · 采集快照');
    const rows = robot?.health?.motor_map?.rows || [];
    const configuration = robot?.monitoring_configuration;
    const declared = configuration?.source === 'unconfigured' ? [] : configuration?.configuration?.joints || [];
    const names = window.rp1RobotView?.joints(robot) || (rows.length ? rows.map(row=>[row.joint,row.label]) : declared.length ? declared.map(item=>[item.index,item.joint_name || item.label]) : [...new Set((robot?.catalog || []).filter(item=>item.key.startsWith('joint.')).map(item=>Number(item.key.split('.')[1])))].sort((a,b)=>a-b).map(index=>[index,`关节 ${index}`]));
    const signature = JSON.stringify([robot?.robot_id,names]);
    if (signature !== jointSignature) {
      jointSignature = signature; const previous = el('joints-page-select').value;
      el('joints-page-select').replaceChildren(...names.map(([joint,label])=>new Option(`J${String(joint).padStart(2,'0')} · ${label}`,String(joint))));
      if (names.some(([joint])=>String(joint) === previous)) el('joints-page-select').value = previous;
      if (!names.length) el('joints-page-select').add(new Option('尚未提供关节配置',''));
    }
    const index = Number(el('joints-page-select').value), motor = health?.motors?.items?.find(item=>item.joint === index);
    text('joints-page-temperature',number(motor?.temperature,'°C')); text('joints-page-mos',number(motor?.mos_temperature,'°C'));
    const torque = robot?.metrics?.[`joint.${index}.torque`];
    const elapsed = window.rp1RobotView?.observationAgeMs(robot) ?? (robot?.transport ? Number.isFinite(robot.transport.elapsed_seconds) ? robot.transport.elapsed_seconds*1000 : Infinity : ((robot?.age_seconds||0)+Math.max(0,robot?.clock_offset_seconds||0))*1000);
    const fresh = available && robot?.online && realtimeAllowed && torque && Number.isFinite(torque.age_ms) && torque.age_ms+elapsed <= 1000;
    text('joints-page-torque',number(fresh ? torque.last : null,'N·m',3));
    if (!available) for (const kind of kinds) text(kind+'-status','平台连接中断，实时读数未知；已有曲线为查询快照。');
    if (active() && !document.hidden) refresh();
  }
  function render(data) {
    const metrics = new Map(data.curves.map(curve=>[curve.metric,curve]));
    const draw = (prefix,key) => {
      const curve = metrics.get(key) || {points:[]};
      text(prefix+'-resolution','当前范围尚无有效数据');
      drawCurve(curve,prefix+'-chart',prefix+'-empty',prefix+'-resolution');
    };
    if (data.component === 'battery') {
      for (const quantity of ['soc','temperature','voltage','current']) draw('battery-'+quantity,'battery.'+quantity);
      if (robot?.source_kind === 'ros2' && robot?.quality?.device_profile?.bms_type === 'journal' && !metrics.get('battery.temperature')?.points?.some(point=>Number.isFinite(point.last))) text('battery-temperature-empty','当前 journal BMS 日志未提供电池温度；不以主机温度代替，缺失不补零。');
      text('battery-page-current-peak',number(data.stats.current_peak_abs,'A',3));
      text('battery-page-voltage-range',`${number(data.stats.voltage_min,'V',3)} / ${number(data.stats.voltage_max,'V',3)}`);
      text('battery-current-extrema',`电流最低 ${number(data.stats.current_min,'A',3)} · 最高 ${number(data.stats.current_max,'A',3)}`);
    } else if (data.component === 'joints') {
      const base = `joint.${data.joint}.`;
      for (const [suffix,metric] of [['temperature','motor_temperature'],['mos','mos_temperature'],['torque','torque'],['rms','tracking_rms']]) draw('joints-'+suffix,base+metric);
      text('joints-page-rms',number(data.stats.tracking_rms,'rad',5));
      text('joints-page-temperature-max',`${number(data.stats.motor_temperature_max,'°C')} / ${number(data.stats.mos_temperature_max,'°C')}`);
      text('joints-page-torque-peak',number(data.stats.torque_peak_abs,'N·m',3));
      text('joints-rms-note',`${data.stats.tracking_samples || 0} 个有效配对样本 · 策略目标对照`);
      if (!data.stats.tracking_samples) text('joints-rms-empty','尚无同步反馈/策略目标配对样本；不从摘要末值推算 RMS。');
    } else {
      draw('host-cpu','host.cpu.usage'); draw('host-memory','host.memory.usage');
      const thermal = data.thermal_metrics || data.curves.filter(curve=>curve.metric.startsWith('host.thermal.')).map(curve=>curve.metric);
      const selector = el('host-thermal-select'), previous = selector.value;
      const signature = thermal.join('|');
      if (selector.dataset.signature !== signature) {
        selector.dataset.signature = signature;
        selector.replaceChildren(...thermal.map(metric=>new Option(robot?.health?.host?.sensors?.find(sensor=>sensor.key === metric)?.label || metric,metric)));
        if (thermal.includes(previous)) selector.value = previous;
        if (!thermal.length) selector.add(new Option('未提供热区数据',''));
      }
      draw('host-temperature',data.selected_thermal || selector.value);
    }
  }
  async function refresh(force=false) {
    if (!active() || document.hidden || !available || !robot || robot.robot_id !== selected) return;
    const kind = page(), joint = kind === 'joints' ? el('joints-page-select').value : '', range = el(kind+'-range').value;
    if (kind === 'joints' && !joint) return;
    const thermal = kind === 'host' ? el('host-thermal-select').value : '';
    const key = [selected,kind,joint,range,thermal].join('|');
    if (inflight === key) return;
    const interval = Number(range) > 3600 ? 60000 : Number(range) > 600 ? 30000 : 10000;
    if (!force && activeKey === key && Date.now()-lastRead < interval) return;
    if (activeKey !== key) clearCurves();
    activeKey = key; inflight = key; lastRead = Date.now();
    el(kind+'-refresh').disabled = true;
    const ticket = ++generation, id = selected;
    if (!result || force) text(kind+'-status','正在读取所选时段的曲线和统计…');
    const timing = window.rp1RobotView?.historyRange(robot,Number(range));
    const end = timing?.end || new Date(), start = timing?.start || new Date(end.getTime()-Number(range)*1000);
    const params = new URLSearchParams({component:kind,start:start.toISOString(),end:end.toISOString(),max_points:'300'});
    if (kind === 'joints') params.set('joint',joint);
    if (thermal) params.set('thermal',thermal);
    try {
      const data = await api(`api/v1/robot-monitors/${encodeURIComponent(id)}/component-history?${params}`,30000);
      if (ticket !== generation || selected !== id || page() !== kind || !active()) return;
      if (data.robot_id !== id || data.component !== kind || (kind === 'joints' && String(data.joint) !== joint)) throw new Error('返回证据与当前设备或关节不一致，请刷新');
      result = data; if (!document.hidden) render(data);
      const source = data.source_kind === 'simulated' ? '模拟数据 · 非实机' : 'ROS2来源 · 待实机验收';
      const note = kind === 'joints' ? '仅有效同采样配对参与跟踪 RMS；策略目标不是最终电机目标。' : '时段统计来自全部原始摘要；曲线保留缺口。';
      text(kind+'-status',`${id} · ${source} · ${new Date(data.start).toLocaleString('zh-CN',{hour12:false})} 至 ${new Date(data.end).toLocaleTimeString('zh-CN',{hour12:false})} · ${timing?.basis || '中央查询时间'} · ${note}`);
    } catch(error) {
      if (ticket !== generation || selected !== id || page() !== kind) return;
      clearCurves(); text(kind+'-status',`查询失败：${error.message}。可点击“刷新数据”重试。`);
    } finally { if (ticket === generation) { inflight = null; lastRead = Date.now(); el(kind+'-refresh').disabled = false; } }
  }
  function activate(view) {
    generation++; inflight = null;
    for (const kind of kinds) el(kind+'-refresh').disabled = false;
    if (kinds.includes(view)) refresh(true);
  }
  function selectionChanged() {
    generation++; inflight = null; activeKey = ''; lastRead = 0; clearCurves();
    for (const kind of kinds) el(kind+'-refresh').disabled = false;
  }
  for (const kind of kinds) {
    el(kind+'-refresh').addEventListener('click',()=>refresh(true));
    el(kind+'-range').addEventListener('change',()=>{selectionChanged(); refresh(true);});
  }
  el('joints-page-select').addEventListener('change',()=>{selectionChanged(); update(robot,available); refresh(true);});
  el('host-thermal-select').addEventListener('change',()=>{selectionChanged(); refresh(true);});
  document.addEventListener('visibilitychange',()=>{if (!document.hidden) refresh(true);});
  new ResizeObserver(()=>{if (!document.hidden && result && active() && result.component === page()) render(result);}).observe(el('monitor-content'));
  window.rp1Components = {update,changed,activate,refresh:()=>refresh(true)};
  setInterval(()=>{refresh();window.rp1HostCores?.tick();},1000);
})();
