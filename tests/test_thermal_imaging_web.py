"""The thermal overlay changes presentation while preserving observed evidence."""

import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest


class TemperatureControls(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.ids = {}
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids[attrs["id"]] = (tag, attrs)


def test_temperature_modes_use_native_keyboard_buttons_and_pressed_state():
    web = Path(__file__).parents[1] / "src/rp1_monitor/web"
    controls = TemperatureControls((web / "index.html").read_text())
    for name, pressed in (("infrared", "true"), ("red", "false")):
        tag, attributes = controls.ids[f"thermal-mode-{name}"]
        assert tag == "button"
        assert attributes["type"] == "button"
        assert attributes["aria-pressed"] == pressed
    assert controls.ids["motor-map"][1]["data-thermal-mode"] == "infrared"


@pytest.mark.skipif(
    not shutil.which("node"), reason="Node exercises the actual thermal overlay DOM"
)
def test_thermal_palette_preserves_limits_unknowns_and_keyboard_drilldown():
    web = Path(__file__).parents[1] / "src/rp1_monitor/web"
    script = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const root=process.argv[1],elements={},opened=[];
class Element {
 constructor(tag='div',namespaceURI='http://www.w3.org/1999/xhtml'){
  this.tagName=tag;this.namespaceURI=namespaceURI;this.dataset=new Proxy({},{set:(o,k,v)=>(o[k]=String(v),true)});this.children=[];this.attributes={};this.events={};
  this.textContent='';this.value='';this.scrollTop=21;this.classes=new Set();
  this.classList={add:(...v)=>v.forEach(x=>this.classes.add(x)),remove:(...v)=>v.forEach(x=>this.classes.delete(x)),contains:x=>this.classes.has(x),toggle:(x,on)=>on?this.classes.add(x):this.classes.delete(x)};
  this.style={setProperty:(name,value)=>{this.style[name]=String(value);}};
 }
 append(...children){for(const child of children){child.parentNode=this;this.children.push(child);}}
 prepend(...children){for(const child of children)child.parentNode=this;this.children.unshift(...children);}
 insertBefore(child,before){child.parentNode=this;const i=this.children.indexOf(before);this.children.splice(i<0?this.children.length:i,0,child);}
 replaceChildren(...children){this.children=[];this.append(...children);}
 remove(){if(this.parentNode)this.parentNode.children=this.parentNode.children.filter(x=>x!==this);}
 setAttribute(key,value){this.attributes[key]=String(value);if(key==='id')elements[String(value)]=this;}
 getAttribute(key){return this.attributes[key]??null;}removeAttribute(key){delete this.attributes[key];}
 addEventListener(type,fn){(this.events[type]||=[]).push(fn);}
 fire(type,extra={}){const event={key:'',prevented:false,preventDefault(){this.prevented=true;},...extra};for(const fn of this.events[type]||[])fn(event);return event;}
 querySelectorAll(selector){const output=[];const walk=node=>{for(const child of node.children){if(matches(child,selector))output.push(child);walk(child);}};walk(this);return output;}
 querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
 closest(selector){let node=this;while(node){if(matches(node,selector))return node;node=node.parentNode;}return null;}focus(){this.focused=true;}
}
function matches(node,selector){
 if(selector.includes(','))return selector.split(',').some(part=>matches(node,part.trim()));
 const id=selector.match(/^#(.+)$/);if(id)return node.getAttribute('id')===id[1];
 if(selector.startsWith('.'))return(node.className||'').split(' ').includes(selector.slice(1));
 const data=selector.match(/^(\w+)?\[data-([\w-]+)(?:=["']?([^\]"']+)["']?)?\]$/);
 if(data){const key=data[2].replace(/-([a-z])/g,(_,x)=>x.toUpperCase());return(!data[1]||node.tagName===data[1])&&key in node.dataset&&(data[3]===undefined||String(node.dataset[key])===data[3]);}
 return node.tagName.toLowerCase()===selector.toLowerCase();
}
const get=id=>elements[id]||(elements[id]=new Element());
const svg=get('motor-map');svg.tagName='svg';svg.namespaceURI='http://www.w3.org/2000/svg';
svg.append(get('motor-map-regions'));const panel=new Element('section');panel.className='motor-map-panel';panel.append(svg);panel.dataset.thermalMode='infrared';svg.dataset.thermalMode='infrared';
get('thermal-mode-infrared').setAttribute('aria-pressed','true');get('thermal-mode-red').setAttribute('aria-pressed','false');
const document={baseURI:'http://monitor.test/',hidden:true,activeElement:null,getElementById:get,
 createElement:tag=>new Element(tag),createElementNS:(namespace,tag)=>new Element(tag,namespace),addEventListener(){},
 querySelector:selector=>selector==='.motor-map-panel'?panel:null,
 querySelectorAll:selector=>selector==='[data-thermal-mode]'?[panel,svg]:[]};
const ctx={console,URL,URLSearchParams,Date,AbortSignal,document,window:{rp1Thermal:{openRegion:key=>opened.push(key),openJoint:key=>opened.push(key)}},
 ResizeObserver:class{observe(){}},setInterval(){},fetch(){throw new Error('Rendering must not request data');}};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(root,'app.js'),'utf8'),ctx,{filename:'app.js'});
vm.runInContext(fs.readFileSync(path.join(root,'thermal-imaging.js'),'utf8'),ctx,{filename:'thermal-imaging.js'});
const api=ctx.window.rp1TemperatureView;assert.equal(typeof api.setMode,'function');assert.equal(typeof api.enhance,'function');
const region=(key,value,state,color)=>({key,label:key,x:0.4,y:0.5,state,value,color,text_color:'#142441',detail:`${key} · 电机 ${value??'未知'} °C · 限值 70 °C`});
const view={summary:'模拟温度',mapping_note:'模拟部位',mapping_coverage:'3/3',meaning:'仅监控不控制电机',scale_min_c:20,scale_max_c:100,hot_count:1,
 regions:[region('left_knee',85,'hot','#be293d'),region('right_knee',35,'below','#f3ccd0'),region('left_ankle',null,'unknown','#c5ceda')],
 rows:[{joint:0,label:'左膝',state:'hot',status:'超温',value:85,color:'#be293d',detail:'电机 85.0 °C / 限值 70 °C',readings:[{value:85,limit_c:70}]}]};
const unchanged=JSON.stringify(view);
const walk=node=>[node,...node.children.flatMap(walk)];
const groups=()=>get('motor-map-regions').querySelectorAll('g[data-region]');
const ellipse=node=>node.querySelector('ellipse');
const fills=()=>groups().map(node=>ellipse(node).getAttribute('fill'));
const finite=()=>{for(const node of walk(svg))for(const value of Object.values(node.attributes))assert.doesNotMatch(String(value),/NaN|Infinity/);};
const isGrey=fill=>{assert.match(fill,/^#[0-9a-f]{6}$/i);const c=[1,3,5].map(i=>parseInt(fill.slice(i,i+2),16));assert.ok(Math.max(...c)-Math.min(...c)<40,'unknown values must use neutral grey, not a thermal hue');};
ctx.renderMotorMap(view);
assert.equal(svg.dataset.thermalMode,'infrared');assert.equal(get('thermal-mode-infrared').getAttribute('aria-pressed'),'true');
assert.equal(get('thermal-mode-red').getAttribute('aria-pressed'),'false');
assert.ok(walk(svg).some(node=>node.tagName.toLowerCase()==='radialgradient'),'infrared uses real SVG gradient stops');
assert.match(fills()[0],/^url\(/);assert.match(fills()[1],/^url\(/);
isGrey(fills()[2]);
assert.notEqual(groups()[0].querySelectorAll('ellipse')[1].getAttribute('fill'),groups()[1].querySelectorAll('ellipse')[1].getAttribute('fill'),'different measurements use distinct spectral colors');
assert.equal(get('motor-map-rows').children[0].children[0].style.backgroundColor,groups()[0].querySelectorAll('ellipse')[1].getAttribute('fill'));
assert.match(get('thermal-image-range').textContent,/35\.0.*85\.0/);
assert.equal(groups()[0].querySelector('text').textContent,'85°');
assert.equal(groups()[2].querySelector('text').textContent,'—');
assert.match(get('motor-map-rows').children[0].children[1].children[1].textContent,/85\.0.*限值 70/);
assert.match(groups()[0].getAttribute('aria-label'),/85\.0摄氏度/);
assert.equal(groups()[0].getAttribute('role'),'button');assert.equal(groups()[0].getAttribute('tabindex'),'0');
assert.equal(groups()[0].fire('keydown',{key:'Enter'}).prevented,true);
assert.equal(groups()[0].fire('keydown',{key:' '}).prevented,true);assert.deepEqual(opened,['left_knee','left_knee']);finite();
document.activeElement=groups()[0];
get('thermal-mode-red').fire('click');
assert.equal(svg.dataset.thermalMode,'red');assert.deepEqual(fills(),['#be293d','#f3ccd0','#c5ceda']);
assert.equal(groups()[0].focused,true,'palette changes preserve a focused joint');
assert.equal(get('motor-map-rows').children[0].children[0].style.backgroundColor,'#be293d');
assert.equal(get('thermal-mode-red').getAttribute('aria-pressed'),'true');
assert.equal(get('thermal-mode-infrared').getAttribute('aria-pressed'),'false');
assert.equal(groups()[0].querySelector('text').textContent,'85°');
assert.equal(JSON.stringify(view),unchanged,'presentation cannot alter temperatures, limits, or backend evidence');
get('thermal-mode-infrared').fire('click');assert.equal(svg.dataset.thermalMode,'infrared');finite();
ctx.renderMotorMap({...view,regions:[region('left_knee',85,'hot','#be293d'),region('right_knee',1000,'unknown','#c5ceda')]});
isGrey(fills()[1]);assert.match(get('thermal-image-range').textContent,/85\.0.*85\.0/);assert.doesNotMatch(get('thermal-image-range').textContent,/1000/);
const equal={...view,scale_min_c:50,scale_max_c:50,regions:[region('left_knee',50,'below','#eac0c8'),region('right_knee',50,'below','#eac0c8')]};
ctx.renderMotorMap(equal);finite();assert.equal(groups()[0].querySelector('text').textContent,'50°');
ctx.renderMotorMap({...view,regions:[region('left_knee',null,'unknown','#c5ceda'),region('right_knee',null,'unknown','#c5ceda')],rows:[],hot_count:0});
fills().forEach(isGrey);assert.match(get('thermal-image-range').textContent,/未知/);finite();
ctx.renderMotorMap(undefined);
assert.equal(groups().length,0);assert.equal(get('motor-map-rows').children.length,0);
assert.match(get('motor-map-status').textContent,/未知/);assert.doesNotMatch(get('motor-map').getAttribute('aria-label'),/85/);
assert.equal(get('motor-map-rows').scrollTop,21);finite();
process.stdout.write('PASS: infrared/red modes, unknown clearing, equal scale, numeric evidence and keyboard access\n');
"""
    result = subprocess.run(["node", "-e", script, str(web)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "PASS: infrared/red modes" in result.stdout
