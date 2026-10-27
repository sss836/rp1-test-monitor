"""Each mapped motor has its own point; unlocated motors remain explicitly unlocated."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="Node exercises real joint map rendering")
def test_individual_joint_points_preserve_temperature_index_and_mapping_boundaries():
    web = Path(__file__).parents[1] / "src/rp1_monitor/web"
    script = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const elements={},opened=[],root=process.argv[1];
class Element {
 constructor(tag='div',namespaceURI='http://www.w3.org/1999/xhtml'){
  this.tagName=tag;this.namespaceURI=namespaceURI;this.dataset=new Proxy({},{set:(o,k,v)=>(o[k]=String(v),true)});
  this.children=[];this.attributes={};this.events={};this.textContent='';this.value='';this.scrollTop=0;this.hidden=false;this.classes=new Set();
  this.classList={add:(...v)=>v.forEach(x=>this.classes.add(x)),remove:(...v)=>v.forEach(x=>this.classes.delete(x)),contains:x=>this.classes.has(x),toggle:(x,on)=>on?this.classes.add(x):this.classes.delete(x)};
  this.style={setProperty:(k,v)=>{this.style[k]=String(v);}};
 }
 append(...children){for(const node of children){node.parentNode=this;this.children.push(node);}}
 prepend(...children){for(const node of children)node.parentNode=this;this.children.unshift(...children);}
 insertBefore(child,before){child.parentNode=this;const index=this.children.indexOf(before);this.children.splice(index<0?this.children.length:index,0,child);}
 replaceChildren(...children){this.children=[];this.append(...children);}
 remove(){if(this.parentNode)this.parentNode.children=this.parentNode.children.filter(node=>node!==this);}
 setAttribute(k,v){this.attributes[k]=String(v);}getAttribute(k){return this.attributes[k]??null;}removeAttribute(k){delete this.attributes[k];}
 addEventListener(type,fn){(this.events[type]||=[]).push(fn);}
 fire(type,extra={}){const event={key:'',preventDefault(){this.prevented=true;},...extra};for(const fn of this.events[type]||[])fn(event);return event;}
 querySelectorAll(selector){return walk(this).slice(1).filter(node=>matches(node,selector));}
 querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
 closest(selector){let node=this;while(node){if(matches(node,selector))return node;node=node.parentNode;}return null;}focus(){this.focused=true;}
}
const walk=node=>[node,...node.children.flatMap(walk)];
function matches(node,selector){
 if(selector.includes(','))return selector.split(',').some(value=>matches(node,value.trim()));
 if(selector.startsWith('.'))return(node.className||'').split(' ').includes(selector.slice(1));
 const match=selector.match(/^(\w+)?\[data-([\w-]+)(?:=["']?([^\]"']+)["']?)?\]$/);
 if(match){const key=match[2].replace(/-([a-z])/g,(_,c)=>c.toUpperCase());return(!match[1]||node.tagName===match[1])&&key in node.dataset&&(match[3]===undefined||node.dataset[key]===match[3]);}
 return node.tagName.toLowerCase()===selector.toLowerCase();
}
const get=id=>elements[id]||(elements[id]=new Element());
const svg=get('motor-map');svg.tagName='svg';svg.namespaceURI='http://www.w3.org/2000/svg';svg.dataset.thermalMode='infrared';
svg.append(get('motor-map-regions'));const panel=new Element('section');panel.className='motor-map-panel';panel.append(svg);
const document={hidden:true,baseURI:'http://monitor.test/',activeElement:null,getElementById:get,
 createElement:tag=>new Element(tag),createElementNS:(ns,tag)=>new Element(tag,ns),addEventListener(){},querySelectorAll:()=>[]};
const ctx={console,document,URL,URLSearchParams,Date,AbortSignal,window:{rp1Thermal:{openJoint:index=>opened.push(index),openRegion(){throw new Error('A joint point must never substitute the hottest motor of its region');}}},
 ResizeObserver:class{observe(){}},setInterval(){},fetch(){throw new Error('Joint rendering has no network side effects');}};
vm.createContext(ctx);
for(const filename of ['app.js','joint-points.js','thermal-imaging.js'])vm.runInContext(fs.readFileSync(path.join(root,filename),'utf8'),ctx,{filename});
const api=ctx.window.rp1JointMap;assert.equal(typeof api.points,'function');assert.equal(typeof api.renderUnmapped,'function');
const layout=[['left_hip',3],['right_hip',3],['left_knee',1],['right_knee',1],['left_ankle',2],['right_ankle',2],['left_shoulder',3],['right_shoulder',3],['left_elbow',1],['right_elbow',1],['left_wrist',2],['right_wrist',2]];
function configuration(dof){
 const regions=layout.map(([key],index)=>({key,label:key,x:key.startsWith('left')?.35:.65,y:.15+index*.05,state:'hot',value:99,color:'#a50f15',text_color:'#fff',detail:'区域汇总不是每轴温度'}));
 let joint=0;const rows=[];
 for(const [region,count]of layout)for(let axis=0;axis<count;axis++){
  const index=joint++,value=index===0?38:index===1?65:index===2?88:45+index,limit=index===0?60:index===1?70:75;
  rows.push({joint:index,name:`joint_${index}`,label:`关节 ${index} · 轴 ${axis+1}`,region,value,state:value>limit?'hot':'below',status:value>limit?'超温':'未超过配置限值',color:index===0?'#ffe0d9':index===1?'#fba58e':'#bd293c',text_color:'#142441',
   readings:[{metric:`joint.${index}.motor_temperature`,label:'电机',value,limit_c:limit},{metric:`joint.${index}.mos_temperature`,label:'MOS',value:value-2,limit_c:limit+5}],detail:`电机 ${value}.0 °C / 限值 ${limit}.0 °C · MOS ${value-2}.0 °C / 限值 ${limit+5}.0 °C`});
 }
 for(let index=24;index<dof;index++)rows.push({joint:index,name:`joint_${index}`,label:`新增关节 ${index}`,region:null,value:index===27?91:50+index,state:index===27?'hot':'below',status:index===27?'超温':'未超过配置限值',color:'#bd293c',text_color:'#142441',readings:[{metric:`joint.${index}.motor_temperature`,value:index===27?91:50+index,limit_c:80}],detail:'部位映射未配置 · 限值 80 °C'});
 return{dof,rows,regions,scale_min_c:20,scale_max_c:100,hot_count:1,mapping_note:'模拟映射',mapping_coverage:`24/${dof}`,summary:`${dof}自由度`,meaning:'点位为配置关节的二维示意，不表示实时姿态或精确安装点'};
}
const points=()=>get('motor-map-regions').querySelectorAll('g[data-joint]');
const point=index=>points().find(node=>node.dataset.joint===String(index));
const text=node=>walk(node).map(child=>child.textContent).join(' ');
const grey=node=>{const color=node.querySelector('ellipse').getAttribute('fill');assert.match(color,/^#[0-9a-f]{6}$/i);const channels=[1,3,5].map(i=>parseInt(color.slice(i,i+2),16));assert.ok(Math.max(...channels)-Math.min(...channels)<40);};
const finite=()=>{for(const node of walk(svg))for(const value of Object.values(node.attributes))assert.doesNotMatch(value,/NaN|Infinity/);};
const full24=configuration(24),original=JSON.stringify(full24);
assert.equal(api.points(full24).length,24);ctx.renderMotorMap(full24);assert.equal(points().length,24);
for(const index of [0,1,2]){
 const node=point(index);assert.equal(node.getAttribute('role'),'button');assert.equal(node.getAttribute('tabindex'),'0');
 assert.match(text(node),new RegExp(String(full24.rows[index].value)));node.fire('click');
 const row=get('motor-map-rows').querySelectorAll('li[data-joint]').find(item=>item.dataset.joint===String(index));
 assert.match(text(row),new RegExp(`限值 ${full24.rows[index].readings[0].limit_c}`));
}
assert.deepEqual(opened,[0,1,2]);
const coords=[0,1,2].map(index=>{const node=point(index).querySelector('ellipse');return `${node.getAttribute('cx')},${node.getAttribute('cy')}`;});
assert.equal(new Set(coords).size,3,'three hip axes have independent, non-overlapping schematic points');
const colors=[0,1,2].map(index=>point(index).querySelectorAll('ellipse').at(-1).getAttribute('fill'));
assert.equal(new Set(colors).size,3,'hip axes use their own measurements instead of the shared hottest region');
assert.equal(JSON.stringify(full24),original);finite();
ctx.window.rp1TemperatureView.setMode('red');assert.equal(points().length,24);
for(const index of [0,1,2])assert.equal(point(index).querySelector('ellipse').getAttribute('fill'),full24.rows[index].color);
ctx.window.rp1TemperatureView.setMode('infrared');assert.equal(points().length,24);finite();
const full28=configuration(28);ctx.renderMotorMap(full28);assert.equal(points().length,24);
assert.equal(api.points(full28).length,24);assert.equal(points().some(node=>Number(node.dataset.joint)>=24),false,'unmapped joints never acquire guessed body coordinates');
const allNodes=()=>[...new Set(Object.values(elements).flatMap(walk))];
const unmapped=allNodes().filter(node=>node.tagName==='button'&&Number(node.dataset.joint)>=24);
assert.equal(unmapped.length,4,'all unlocated motors have explicit independent buttons');
const last=unmapped.find(node=>node.dataset.joint==='27');assert.ok(last);assert.match(text(last),/91/);last.fire('click');assert.equal(opened.at(-1),27);
const partial=configuration(24);partial.rows[0]={...partial.rows[0],state:'unknown',value:85,color:'#c5ceda',detail:'数据缺失 / 已过期'};
ctx.renderMotorMap(partial);assert.equal(points().length,24);grey(point(0));
assert.equal(api.points({...full24,regions:[]}).length,0,'no available anchor means no invented position');
ctx.renderMotorMap(full24);assert.equal(points().length,24);
assert.equal(allNodes().filter(node=>node.tagName==='button'&&Number(node.dataset.joint)>=24).length,0,'28 to 24 removes old unlocated joints');
ctx.renderMotorMap(undefined);assert.equal(points().length,0);assert.equal(get('motor-map-rows').children.length,0);
assert.equal(allNodes().filter(node=>node.tagName==='button'&&Number(node.dataset.joint)>=24).length,0);finite();
process.stdout.write('PASS: independent 24/28 joint temperatures, precise drilldown and unmapped/unknown isolation\n');
"""
    result = subprocess.run(["node", "-e", script, str(web)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "PASS: independent 24/28 joint temperatures" in result.stdout
