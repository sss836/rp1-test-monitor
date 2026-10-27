"""Read-only temperature drill-down must keep robot, joint and query context aligned."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="Node用于关节温度弹窗DOM回归")
def test_temperature_dialog_selects_hottest_and_discards_stale_evidence():
    source = Path(__file__).parents[1] / "src/rp1_monitor/web/thermal.js"
    script = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Element {
 constructor(){this.children=[];this.dataset={};this.textContent='';this.value='';this.events={};this.open=false;this.disabled=false;this.classes=new Set();this.classList={add:x=>this.classes.add(x),remove:x=>this.classes.delete(x),toggle:(x,yes)=>yes?this.classes.add(x):this.classes.delete(x)};}
 append(...children){this.children.push(...children);}replaceChildren(){this.children=[];}
 addEventListener(type,handler){this.events[type]=handler;}showModal(){this.open=true;}
 close(){this.open=false;this.events.close?.();}
}
const elements={},calls=[],draws=[],diagnoses=[];
const get=id=>elements[id]||(elements[id]=new Element());get('thermal-minutes').value='10';
const rows=[{joint:0,label:'左膝 A',region:'left_knee',value:70,readings:[{value:70}]},{joint:1,label:'左膝 B',region:'left_knee',value:85,readings:[{value:85}]},{joint:27,label:'未映射新增关节',region:null,value:79,readings:[{value:79}]}];
const ctx={console,Number,Math,Date,document:{createElement:()=>new Element()},byId:get,selected:'A',robots:[{robot_id:'A',name:'模拟 A',source_kind:'simulated',health:{motor_map:{rows}}},{robot_id:'B',name:'模拟 B',source_kind:'simulated',health:{motor_map:{rows:[rows[0]]}}}],
 window:{rp1Operations:{temperatureContext:(...args)=>diagnoses.push(args)}},
 api:path=>new Promise((resolve,reject)=>calls.push({path,resolve,reject})),drawCurve:(...args)=>draws.push(args)};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
const settle=()=>new Promise(setImmediate);
function response(robot,joint,label=`关节 ${joint}`){return {robot_id:robot,joint,source_kind:'simulated',as_of_at:'2026-10-23T04:05:06Z',configuration_revision:7,start:'2026-10-23T03:55:06Z',end:'2026-10-23T04:05:06Z',current:{label,state:'hot',status:'超温',detail:'仅模拟观测',readings:[{metric:`joint.${joint}.motor_temperature`,label:'电机',value:85,limit_c:60},{metric:`joint.${joint}.mos_temperature`,label:'MOS',value:null,limit_c:65}]},curves:[{robot_id:robot,metric:`joint.${joint}.motor_temperature`,points:[{time:'2026-10-23T04:05:05Z',last:null,missing_windows:1},{time:'2026-10-23T04:05:06Z',last:85}]},{robot_id:robot,metric:`joint.${joint}.mos_temperature`,points:[]}],alerts:{heads:[{rule_key:`limit.joint.${joint}.motor_temperature`,state:'active',is_open:true,evaluation_fresh:false,opened_at:'2026-10-23T04:00:00Z',condition_since:'2026-10-23T03:59:56Z',evaluated_at:'2026-10-23T04:00:00Z',evidence:{detail:'<script>原样文本证据</script>'}}],events:[{event_kind:'opened',occurred_at:'2026-10-23T04:00:00Z',rule_key:`limit.joint.${joint}.motor_temperature`,evidence:{detail:'持续条件确认事件'}}]}};}
(async()=>{
 ctx.window.rp1Thermal.openRegion('left_knee');assert.match(calls[0].path,/A\/joints\/1\/temperature/);assert.equal(get('thermal-joint').children.length,3);
 assert.match(get('thermal-joint').children[2].textContent,/部位待确认/);
 ctx.window.rp1Thermal.openJoint(27);assert.match(calls[1].path,/A\/joints\/27\/temperature/);
 calls[0].resolve(response('A',1,'旧关节响应'));await settle();assert.equal(get('thermal-readings').children.length,0);
 calls[1].resolve(response('A',27,'<img onerror=alert(1)>'));await settle();
 assert.equal(get('thermal-title').textContent,'<img onerror=alert(1)> · 温度详情');assert.equal(get('thermal-source').textContent,'模拟数据 · 非实机');
 assert.equal(get('thermal-readings').children[0].children[1].textContent,'85.0 °C');assert.equal(get('thermal-readings').children[1].children[3].textContent,'未知 · 无有效观测');
 const alert=get('thermal-alert-heads').children[0];assert.match(alert.children[0].children[1].textContent,/当前未知/);assert.equal(alert.children[2].textContent,'<script>原样文本证据</script>');
 assert.equal(draws.length,2);assert.equal(draws[0][0].points[0].last,null);assert.equal(draws[0][0].points[0].missing_windows,1);
 get('thermal-diagnose').events.click();assert.equal(get('thermal-dialog').open,false);assert.equal(diagnoses.length,1);assert.deepEqual(diagnoses[0],[27,10]);assert.equal(calls.length,2);
 // Switch robots while an A request is in flight, then open B. Late A must remain invisible.
 ctx.window.rp1Thermal.openJoint(1);assert.equal(calls.length,3);ctx.selected='B';ctx.window.rp1Thermal.changed();assert.equal(get('thermal-dialog').open,false);
 ctx.window.rp1Thermal.openJoint(0);assert.equal(calls.length,4);calls[3].resolve(response('B',0,'机器人 B 当前关节'));await settle();
 const currentTitle=get('thermal-title').textContent,currentEvents=get('thermal-events').children.length;
 calls[2].resolve(response('A',1,'错误的机器人 A'));await settle();assert.equal(get('thermal-title').textContent,currentTitle);assert.match(currentTitle,/机器人 B/);assert.equal(get('thermal-events').children.length,currentEvents);
 // A query-range change also clears old evidence; only its matching response may re-enable diagnosis.
 get('thermal-minutes').value='60';get('thermal-minutes').events.change();assert.match(calls[4].path,/minutes=60/);assert.equal(get('thermal-diagnose').disabled,true);assert.equal(get('thermal-readings').children.length,0);
 calls[4].reject(new Error('连接中断'));await settle();assert.equal(get('thermal-diagnose').disabled,true);assert.match(get('thermal-query-status').textContent,/未知/);assert.equal(get('thermal-motor-chart').children.length,0);
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    subprocess.run(
        ["node", "-e", script, str(source)],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.skipif(not shutil.which("node"), reason="Node用于诊断联动初始化DOM回归")
def test_operations_exports_temperature_context_and_prefills_without_network():
    source = Path(__file__).parents[1] / "src/rp1_monitor/web/operations.js"
    script = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Element {
 constructor(){this.children=[];this.events={};this.value='';this.textContent='';this.open=false;}
 append(...children){this.children.push(...children);}replaceChildren(...children){this.children=[...children];}
 add(child){this.children.push(child);}get options(){return this.children;}
 addEventListener(type,handler){this.events[type]=handler;}querySelectorAll(){return [];}
 showModal(){this.open=true;}focus(){this.focused=true;}
}
const elements={},requests=[];const el=id=>elements[id]||(elements[id]=new Element());
const ctx={console,document:{getElementById:el,querySelectorAll:()=>[]},window:{},selected:'A',robots:[{robot_id:'A',source_kind:'simulated',catalog:[]}],
 Option:class Option{constructor(label,value){this.label=label;this.value=value;}},fetch:(...args)=>{requests.push(args);throw new Error('Unexpected request during prefill');}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
assert.equal(typeof ctx.window.rp1Operations.changed,'function');assert.equal(typeof ctx.window.rp1Operations.temperatureContext,'function');
ctx.window.rp1Operations.temperatureContext(27,60);assert.equal(requests.length,0);
assert.equal(el('ops-dialog').open,true);assert.equal(el('ops-minutes').value,'60');assert.equal(el('ops-metric').value,'joint.27.motor_temperature');
assert.match(el('ops-question').value,/关节 27/);assert.match(el('ops-message').textContent,/尚未调用模型/);
ctx.window.rp1Operations.temperatureContext(1,17);assert.equal(el('ops-minutes').value,'10');assert.equal(requests.length,0);
"""
    subprocess.run(
        ["node", "-e", script, str(source)],
        check=True,
        capture_output=True,
        text=True,
    )
