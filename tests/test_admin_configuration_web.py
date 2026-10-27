"""Exercise configuration editing and async conflicts without browser dependencies."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="Node用于管理员配置DOM回归")
def test_admin_configuration_preserves_drafts_and_rejects_stale_contexts():
    source = Path(__file__).parents[1] / "src/rp1_monitor/web/admin.js"
    script = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const code=source.slice(source.indexOf('function emptyConfiguration('),source.indexOf("$('login-form').addEventListener"));
class Element {
  constructor(tag='div'){this.tag=tag;this.children=[];this.dataset={};this.attributes={};this.events={};this.textContent='';this._value='';this.checked=false;this.disabled=false;this.classes=new Set();this.classList={add:x=>this.classes.add(x),remove:x=>this.classes.delete(x),toggle:(x,yes)=>yes?this.classes.add(x):this.classes.delete(x)};}
  set value(value){this._value=String(value);}get value(){return this._value;}get valueAsNumber(){return this.value===''?NaN:Number(this.value);}
  setAttribute(name,value){this.attributes[name]=value;}
  append(...children){this.children.push(...children);}replaceChildren(){this.children=[];}
  addEventListener(type,handler){this.events[type]=handler;}focus(){this.focused=true;}setCustomValidity(value){this.validation=value;}
  querySelector(selector){const field=selector.match(/^\[data-field="(.+)"\]$/)?.[1];let found=null;const walk=node=>{for(const child of node.children){if(child.dataset.field===field&&field)found=child;else walk(child);}};walk(this);return found;}
}
function harness(){
 const elements={},messages=[];
 const ctx={Number,Object,Set,JSON,structuredClone,URLSearchParams,console,document:{createElement:tag=>new Element(tag)},
  $:id=>elements[id]||(elements[id]=new Element()),element:(tag,text,css)=>{const el=new Element(tag);if(text!==undefined)el.textContent=text;if(css)el.className=css;return el;},
  date:value=>value,current:()=>({source_kind:'simulated'}),window:{confirm:()=>true},message:text=>messages.push(text),reason:id=>elements[id].value,
  request:async()=>({items:[],next_before_id:null})};
 ctx.busy=(_form,fn)=>{ctx.pending=fn();return ctx.pending;};
 vm.createContext(ctx);
 vm.runInContext("let configurationRevision=null,configurationDraft=null,configurationRobot=null,configurationOriginalDof=24,configurationDirty=false,configurationReady=false,configurationConflict=false,configurationRequest=0,configurationVersionsRequest=0,configurationNext=null;let selected='A',generation=1;const regions=[['left_knee','左膝']];",ctx);
 vm.runInContext(code,ctx);return{ctx,elements,messages};
}
(async()=>{
 // Real form rows retain hidden 28-DOF entries while switching and send only the selected model.
 const first=harness(),{ctx,elements}=first;
 const configuration=ctx.emptyConfiguration(28);
 configuration.joints[0].label='<img onerror=alert(1)>';
 configuration.joints[27].label='新增关节';configuration.joints[27].bus='can1';configuration.joints[27].motor_id='4';
 configuration.temperature_limits_c['joint.27.motor_temperature']=65;
 ctx.renderConfiguration({robot_id:'A',revision:9,configuration,source:'database'});
 assert.equal(elements['configuration-joints'].children.length,28);
 assert.equal(elements['configuration-joints'].children[0].querySelector('[data-field="label"]').value,'<img onerror=alert(1)>');
 elements['configuration-joints'].children[0].querySelector('[data-field="label"]').value='左膝核对';
 elements['configuration-dof'].value='24';elements['configuration-dof'].events.change();
 assert.equal(elements['configuration-joints'].children.length,24);
 assert.equal(elements['configuration-reduction-confirm'].required,true);
 let result=ctx.validateConfiguration();assert.equal(result.joints[0].label,'左膝核对');assert.equal(result.joints.length,24);
 assert.equal(Object.keys(result.temperature_limits_c).length,0);
 elements['configuration-dof'].value='28';elements['configuration-dof'].events.change();
 assert.equal(elements['configuration-joints'].children[27].querySelector('[data-field="label"]').value,'新增关节');
 result=ctx.validateConfiguration();assert.equal(result.temperature_limits_c['joint.27.motor_temperature'],65);
 // Unconfigured 24 -> 28 is complete but does not invent mappings or safety limits.
 ctx.renderConfiguration({robot_id:'A',revision:null,configuration:ctx.emptyConfiguration(24),source:'unconfigured'});
 elements['configuration-dof'].value='28';elements['configuration-dof'].events.change();result=ctx.validateConfiguration();
 assert.equal(result.joints.length,28);assert.equal(result.joints[27].region,null);assert.deepEqual(Object.keys(result.temperature_limits_c),[]);
 assert.match(elements['configuration-source'].textContent,/尚未确认/);
 // Mapping confirmation cannot bypass actual ROS names, nor can incomplete motor identities pass.
 ctx.current=()=>({source_kind:'ros2'});elements['configuration-mapping-verified'].checked=true;
 assert.throws(()=>ctx.validateConfiguration(),/实际关节名称/);
 elements['configuration-mapping-verified'].checked=false;ctx.current=()=>({source_kind:'simulated'});
 const firstRow=elements['configuration-joints'].children[0];firstRow.querySelector('[data-field="bus"]').value='can0';
 assert.throws(()=>ctx.validateConfiguration(),/一起填写/);firstRow.querySelector('[data-field="motor_id"]').value='1';
 const secondRow=elements['configuration-joints'].children[1];secondRow.querySelector('[data-field="bus"]').value='can0';secondRow.querySelector('[data-field="motor_id"]').value='1';
 assert.throws(()=>ctx.validateConfiguration(),/编号重复/);secondRow.querySelector('[data-field="motor_id"]').value='2';
 elements['configuration-scale-min'].value='101';assert.throws(()=>ctx.validateConfiguration(),/色阶起点/);elements['configuration-scale-min'].value='20';
 // A concurrent configuration write returns 409; keep edits and prevent an automatic overwrite.
 const calls=[];ctx.request=async(path,options)=>{calls.push({path,options});const error=new Error('conflict');error.status=409;throw error;};
 elements['configuration-joints'].children[0].querySelector('[data-field="label"]').value='当前未保存内容';
 elements['configuration-reason'].value='依据模拟资料验证';
 elements['configuration-form'].events.submit({preventDefault(){},currentTarget:elements['configuration-form']});await ctx.pending;
 assert.equal(calls.length,1);assert.equal(JSON.parse(calls[0].options.body).expected_revision,null);
 assert.equal(elements['configuration-joints'].children[0].querySelector('[data-field="label"]').value,'当前未保存内容');
 assert.match(elements['configuration-error'].textContent,/仍保留/);
 elements['configuration-form'].events.submit({preventDefault(){},currentTarget:elements['configuration-form']});
 await assert.rejects(ctx.pending,/中央版本已改变/);assert.equal(calls.length,1);
 // Late responses from robot A must never replace a newer robot B form or version history.
 const late=harness(),queue=[];late.ctx.request=path=>new Promise(resolve=>queue.push({path,resolve}));
 const pendingA=late.ctx.loadConfiguration();vm.runInContext("selected='B';generation++;",late.ctx);
 const pendingB=late.ctx.loadConfiguration();
 const envelope=id=>({robot_id:id,revision:id==='A'?1:2,configuration:late.ctx.emptyConfiguration(id==='A'?24:28),source:'database'});
 queue[1].resolve(envelope('B'));await new Promise(setImmediate);
 assert.equal(queue.length,3);assert.match(queue[2].path,/robots\/B\/configuration\/versions/);
 queue[2].resolve({items:[],next_before_id:null});await pendingB;
 queue[0].resolve(envelope('A'));await pendingA;
 assert.equal(late.elements['configuration-dof'].value,'28');assert.equal(late.elements['configuration-version'].textContent,'当前版本 #2');
 assert.equal(vm.runInContext('configurationRobot',late.ctx),'B');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    subprocess.run(
        ["node", "-e", script, str(source)],
        check=True,
        capture_output=True,
        text=True,
    )
