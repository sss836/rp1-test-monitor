import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="Node用于温度图DOM回归")
def test_motor_map_keeps_numeric_evidence_and_clears_old_colors(tmp_path):
    from rp1_monitor.health_view import build_health
    from rp1_monitor.motor_map import MotorMapConfig, build_motor_map

    config = MotorMapConfig.model_validate(
        {
            "robots": {
                "SIM": {
                    "source_kind": "simulated",
                    "scale_min_c": 20,
                    "scale_max_c": 100,
                    "joints": [
                        {"index": 0, "label": "<img onerror=alert(1)>", "region": "left_knee"}
                    ],
                }
            }
        }
    )
    health = build_health(
        {"joint.0.motor_temperature": 85}, {"joint.0.motor_temperature": 1}, {}, {}
    )
    fixture = tmp_path / "view.json"
    fixture.write_text(
        json.dumps(
            build_motor_map("SIM", "simulated", health, config, {"joint.0.motor_temperature": 60})
        )
    )
    source = Path(__file__).parents[1] / "src/rp1_monitor/web/app.js"
    script = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const code=fs.readFileSync(process.argv[1],'utf8');
const part=code.slice(code.indexOf('function renderMotorMap('),code.indexOf('function renderProcesses('));
function el(){return {children:[],dataset:{},style:{},attributes:{},namespaceURI:'http://www.w3.org/2000/svg',classList:{toggle(){}},textContent:'',replaceChildren(){this.children=[]},append(...c){this.children.push(...c)},setAttribute(k,v){this.attributes[k]=v}}}
const elements={};const ctx={byId:id=>elements[id]||(elements[id]=el()),setText:(id,text)=>{(elements[id]||(elements[id]=el())).textContent=text},document:{createElement:el,createElementNS:el}};
vm.createContext(ctx);vm.runInContext(part,ctx);
const data=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
ctx.renderMotorMap(data);assert.match(elements['motor-map-status'].textContent,/1 台电机超温/);
const region=elements['motor-map-regions'].children[0];assert.equal(region.dataset.state,'hot');assert.equal(region.children[2].textContent,'85°');
const row=elements['motor-map-rows'].children[0];assert.match(row.children[1].children[0].textContent,/<img/);assert.match(row.children[1].children[1].textContent,/85.0 °C/);assert.equal(row.children[2].textContent,'超温');
ctx.renderMotorMap(null);assert.equal(elements['motor-map-regions'].children.length,0);assert.equal(elements['motor-map-rows'].children.length,0);assert.equal(elements['motor-map-status'].textContent,'温度状态未知');
ctx.renderMotorMap({...data,regions:[],rows:[],hot_count:0,summary:'0 台电机超温'});assert.equal(elements['motor-map-regions'].children.length,0);
"""
    subprocess.run(
        ["node", "-e", script, str(source), str(fixture)],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.skipif(not shutil.which("node"), reason="Node仅用于独立SVG函数回归检查")
def test_partial_bucket_preserves_known_extrema_without_connecting_across_gap():
    source = Path(__file__).parents[1] / "src/rp1_monitor/web/app.js"
    script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const code = fs.readFileSync(process.argv[1], 'utf8');
const part = code.slice(code.indexOf('function drawCurve('), code.indexOf('async function loadCurve('));
const elements = { chart: {clientWidth:1000, clientHeight:270, attributes:{}, setAttribute(k,v){this.attributes[k]=v}, children: [], replaceChildren(){this.children=[]}, append(e){this.children.push(e)}}, 'chart-empty': {classList:{toggle(){}}} };
const ctx = { byId:id=>elements[id], setText:()=>{}, document:{createElementNS:(_ns,tag)=>({tag,attributes:{},setAttribute(k,v){this.attributes[k]=v}})} };
vm.createContext(ctx); vm.runInContext(part, ctx);
ctx.drawCurve({unit:'N·m',points:[{time:'2026-01-01T00:00:00Z',last:2,minimum:1,maximum:99,missing_windows:1,gap_count:0},{time:'2026-01-01T00:00:01Z',last:3,minimum:2,maximum:3,missing_windows:0,gap_count:0}]});
const children = elements.chart.children;
assert.equal(children.filter(x=>x.attributes.class==='range').length,2);
assert.equal(children.filter(x=>x.tag==='circle').length,1);
const path = children.find(x=>x.tag==='path').attributes.d;
assert.equal((path.match(/M/g)||[]).length,2); assert.equal(path.includes('L'),false);
elements.chart.clientWidth=300;
ctx.drawCurve({unit:'rad',points:[{time:'2026-01-01T00:00:00Z',last:2,minimum:1,maximum:3,missing_windows:0,gap_count:0}]});
assert.equal(elements.chart.attributes.viewBox,'0 0 272 265');
assert.equal(elements.chart.children.filter(x=>x.tag==='text').length,8);
"""
    subprocess.run(["node", "-e", script, str(source)], check=True, capture_output=True, text=True)


@pytest.mark.skipif(not shutil.which("node"), reason="Node用于独立DOM状态回归")
def test_process_view_discards_live_resources_when_platform_disconnects():
    source = Path(__file__).parents[1] / "src/rp1_monitor/web/app.js"
    script = r"""
const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
const code=fs.readFileSync(process.argv[1],'utf8');
const part=code.slice(code.indexOf('function renderProcesses('),code.indexOf('async function api('));
function el(){return {children:[],textContent:'',replaceChildren(){this.children=[]},append(child){this.children.push(child)}};}
const elements={'process-summary':el(),'process-body':el(),'process-evidence':el()};
const ctx={byId:id=>elements[id],setText:(id,text)=>elements[id].textContent=text,document:{createElement:()=>el()}};
vm.createContext(ctx);vm.runInContext(part,ctx);
const robot={source_kind:'simulated',health:{processes:{items:[{label:'<img onerror=alert(1)>',comm:'motion',state:'running',label_state:'存活',cpu_percent:10,rss_mib:68,threads:4,observed_restarts:1,age_ms:200,pid:42,detail:'fixture',observed_at:'2026-01-01T00:00:00Z'}]}}};
ctx.renderProcesses(robot);let cells=elements['process-body'].children[0].children;
assert.match(elements['process-summary'].textContent,/模拟数据/);assert.match(cells[0].textContent,/<img/);
assert.equal(cells[2].textContent,'10.0%');assert.equal(cells[3].textContent,'68.0');
ctx.renderProcesses(robot,false);cells=elements['process-body'].children[0].children;
assert.equal(cells[1].textContent,'未知');assert.equal(cells[2].textContent,'—');assert.equal(cells[3].textContent,'—');assert.equal(cells[6].textContent,'—');
assert.match(elements['process-summary'].textContent,/连接中断/);assert.match(elements['process-evidence'].children[0].textContent,/旧观测/);
"""
    subprocess.run(["node", "-e", script, str(source)], check=True, capture_output=True, text=True)


@pytest.mark.skipif(not shutil.which("node"), reason="Node用于中央大屏异步上下文回归")
def test_central_dashboard_does_not_mix_delayed_responses_from_other_robot():
    source = Path(__file__).parents[1] / "src/rp1_monitor/web/central.js"
    script = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
function el(){return {children:[],textContent:'',replaceChildren(){this.children=[]},append(...c){this.children.push(...c)},prepend(c){this.children.unshift(c)},addEventListener(){}};}
const elements={},calls=[];
const context={selected:'A',robots:[{robot_id:'A',source_kind:'simulated'},{robot_id:'B',source_kind:'simulated'}],window:{},document:{hidden:false,createElement:el},setInterval:()=>{},
 byId:id=>elements[id]||(elements[id]=el()),setText:(id,text)=>{(elements[id]||(elements[id]=el())).textContent=text},api:path=>new Promise(resolve=>calls.push({path,resolve}))};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
assert.equal(calls.length,4);context.selected='B';context.window.rp1Central.changed();assert.equal(calls.length,8);
function finish(offset,id){calls[offset].resolve({items:[{state:'unknown',rule_key:'process.motion.unavailable',is_open:true,evaluation_fresh:false,evidence:{detail:'<script>text only</script>'}}]});
 calls[offset+1].resolve({items:[]});calls[offset+2].resolve({items:[{robot_id:id,windows:1,raw_json_bytes:100,key_json_bytes:10,estimated_30day_1hz_json_bytes:{raw:1000,key:100}}],physical_bytes:200,physical_scope:'test',days:[],policy:{},policy_sha256:'test',meaning:'fixture',central_disk:{detail:'中央盘未知'}});
 calls[offset+3].resolve({items:[],retention_days:30});}
(async()=>{finish(4,'B');await new Promise(setImmediate);assert.match(elements['central-status'].textContent,/B/);assert.match(elements['central-alert-count'].textContent,/过期/);
 const summary=elements['storage-device'].textContent;finish(0,'A');await new Promise(setImmediate);assert.match(elements['central-status'].textContent,/B/);assert.equal(elements['storage-device'].textContent,summary);
 assert.equal(elements['central-alerts'].children[0].children[1].textContent,'<script>text only</script>');})().catch(e=>{console.error(e);process.exitCode=1});
"""
    subprocess.run(["node", "-e", script, str(source)], check=True, capture_output=True, text=True)
