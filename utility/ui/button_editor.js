(function () {
  const modes = ['normal','terminal','extra1','extra2'];
  const buttons = ['volume_up','volume_down','play_pause'];
  const gestures = ['', '_double', '_hold', '_hold_release'];
  const actions = ['start','commands','mic_off','hush','send','recognize_send','clear','next','previous','start_send','normal_mode','terminal_mode','extra1_mode','extra2_mode'];
  const copy = x => JSON.parse(JSON.stringify(x));
  const list = x => [...new Set((Array.isArray(x)?x:[x]).filter(v=>actions.includes(v)))];

  function readProfiles(controls) {
    const profiles = {};
    for (const mode of modes) {
      const saved = controls.profiles?.[mode] || {};
      const profile = profiles[mode] = {};
      for (const button of buttons) {
        profile[button + '_repeat'] = list(saved[button + '_repeat']);
        profile[button + '_gap_ms'] = saved[button + '_gap_ms'] ?? 1000;
        for (const suffix of gestures) {
          const key = button + suffix;
          let legacy;
          if (mode === 'normal') {
            legacy = list(controls[key]);
            if (!suffix.includes('hold')) legacy = list([...legacy, ...list(controls[key + '_second'])]);
          } else {
            const fallback = mode === 'terminal' ? {volume_up:'next', volume_down:'previous', play_pause:'normal_mode'}[key] : undefined;
            legacy = controls.modes?.[mode]?.[key] ?? fallback;
          }
          profile[key] = list(saved[key] ?? legacy);
        }
      }
    }
    return profiles;
  }

  window.GoblinButtonEditor = function(root, changed, translate) {
    let profiles={}, stamp='', language='', active='normal';
    const names=()=>({normal:('Standard mode'),terminal:('Terminal mode'),extra1:('Additional mode 1'),extra2:('Additional mode 2')});
    const actionName=a=>a.endsWith('_mode')?('Switch: ')+names()[{normal_mode:'normal',terminal_mode:'terminal',extra1_mode:'extra1',extra2_mode:'extra2'}[a]]:translate('media.action.'+a);
    const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
    const style=el('style');style.textContent=`.be-profile{border:1px solid #8885;border-radius:14px;margin:12px 0;padding:16px}.be-profile>summary{font-size:18px;font-weight:650;cursor:pointer;display:list-item}.be-active{font-size:12px;color:#69b9ff;margin-left:12px}.be-buttons{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,260px),1fr));gap:12px;margin-top:16px}.be-button{border:1px solid #8884;border-radius:12px;padding:14px;min-width:0}.be-button[data-button=volume_up]{background:#25634a38;border-color:#66b89270}.be-button[data-button=volume_down]{background:#79572838;border-color:#d6ab6570}.be-button[data-button=play_pause]{background:#51417b38;border-color:#a694dc70}.be-button>summary{font-size:20px;font-weight:650;cursor:pointer}.be-action{margin:12px 0}.be-action>strong{display:block;margin-bottom:6px}.be-menu{border:1px solid #8885;border-radius:8px;padding:9px}.be-menu>summary{cursor:pointer;overflow-wrap:anywhere;font-size:13px}.be-options{display:grid;gap:2px;margin-top:8px;max-height:300px;overflow:auto}.be-option{display:flex!important;align-items:center;gap:9px;padding:7px!important;font-size:13px!important;cursor:pointer}.be-option input{flex:0 0 auto!important;width:17px!important;height:17px!important;appearance:auto!important;accent-color:#7095ff}.be-note{font-size:13px;opacity:.7;line-height:1.5}.be-order{font-size:12px;overflow-wrap:anywhere;margin-top:6px;color:#799fee}.be-hold{border-top:1px solid #8884;padding-top:10px;margin-top:12px}.be-clear{font-size:12px;margin:6px 0;padding:5px 8px}`;
    root.append(style);
    const content=el('div');root.append(content);
    function menu(mode,key,title) {
      const block=el('div',undefined,'be-action');block.append(el('strong',title));
      const box=el('details',undefined,'be-menu'),summary=el('summary'),options=el('div',undefined,'be-options'),order=el('div','', 'be-order');
      box.dataset.goblinId=`voice:buttons:${mode}:${key}`;
      const refresh=()=>{const chosen=profiles[mode][key];summary.textContent=chosen.length?('Selected: ')+chosen.length+' · '+chosen.map(actionName).join(', '):('Choose commands — none assigned');order.textContent=chosen.map((a,i)=>`${i+1}. ${actionName(a)}`).join(' → ');};
      for(const action of actions){
        const label=el('label',undefined,'be-option'),check=el('input');check.type='checkbox';check.checked=profiles[mode][key].includes(action);check.dataset.action=action;
        check.addEventListener('change',()=>{const chosen=profiles[mode][key];if(check.checked&&!chosen.includes(action))chosen.push(action);else if(!check.checked)profiles[mode][key]=chosen.filter(a=>a!==action);stamp=JSON.stringify([profiles,language]);refresh();changed();});
        label.append(check,el('span',actionName(action)));options.append(label);
      }
      const clear=el('button',('Clear all'),'g-btn be-clear');clear.type='button';clear.onclick=()=>{profiles[mode][key]=[];options.querySelectorAll('input').forEach(c=>c.checked=false);stamp=JSON.stringify([profiles,language]);refresh();changed();};
      box.append(summary,options,clear);block.append(box,order);refresh();return block;
    }
    function draw(){
      const open=new Set([...content.querySelectorAll('details[open][data-mode]')].map(n=>n.dataset.mode));
      content.replaceChildren(el('p',('Check commands to run them in selection order. An empty list does nothing. Each mode stores independent assignments.'),'be-note'));
      for(const mode of modes){
        const section=el('details',undefined,'be-profile');section.dataset.mode=mode;section.open=open.has(mode)||(!open.size&&mode==='normal');
        const summary=el('summary',names()[mode]),badge=el('span','', 'be-active');badge.dataset.activeMode=mode;summary.append(badge);section.append(summary);
        const grid=el('div',undefined,'be-buttons');
        for(const button of buttons){
          const card=el('details',undefined,'be-button');card.dataset.button=button;card.append(el('summary',button==='volume_up'?'+':button==='volume_down'?'−':'▶ Play'));
          card.append(menu(mode,button,('Single click')),menu(mode,button+'_double',('Double click')));
          const hold=el('div',undefined,'be-hold');hold.append(el('strong',('Hold')),menu(mode,button+'_hold',('While held · after 0.6 s')),menu(mode,button+'_hold_release',('On release')));
          const repeat=el('div',undefined,'be-hold');
          repeat.append(menu(mode,button+'_repeat',('Second press')));
          const label=el('label',('Minimum gap before the second command, s '));
          const gap=el('input'), gapKey=button+'_gap_ms';
          gap.type='number';gap.min='0.35';gap.max='86400';gap.step='0.001';gap.value=profiles[mode][gapKey]/1000;
          gap.onchange=()=>{
            if(!gap.value||!gap.checkValidity()){gap.value=profiles[mode][gapKey]/1000;return;}
            profiles[mode][gapKey]=Math.round(gap.valueAsNumber*1000);
            stamp=JSON.stringify([profiles,language]);changed();
          };
          const hint=el('p',undefined,'be-note');
          const explain=()=>{
            const seconds=gap.valueAsNumber;
            if(!Number.isFinite(seconds))return;
            const pause=seconds.toLocaleString(('en'));
            hint.textContent=(`If a second command is assigned, press once, then press again after at least ${pause} s. The next press starts the cycle again. Two quick presses can trigger Double click. An early second press is ignored if no double click is detected.`);
          };
          gap.addEventListener('input',explain);
          gap.addEventListener('change',explain);
          explain();label.append(gap);repeat.append(label,hint);
          card.append(repeat,hold);grid.append(card);
        }
        section.append(grid);content.append(section);
      }
      showActive(active);
    }
    function showActive(mode){active=mode;content.querySelectorAll('[data-active-mode]').forEach(n=>n.textContent=n.dataset.activeMode===mode?('Active'):'');}
    return {
      fill(controls,lang){
        const next=readProfiles(controls);
        const nextStamp=JSON.stringify([next,lang]);if(stamp===nextStamp)return;profiles=next;language=lang;stamp=nextStamp;draw();
      },
      matches: saved => JSON.stringify(profiles) === JSON.stringify(readProfiles({profiles:saved})),
      value:()=>copy(profiles),
      active:showActive
    };
  };
})();

// Each binding owns all four profiles; live inventories never overwrite edited assignments.
window.GoblinControllersEditor = function(root, changed, translate) {
  const clone=x=>JSON.parse(JSON.stringify(x));
  const node=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;};
  let controls={}, bindings=null, selected='', lang='en', sound={}, defaults=null, inventoryStamp='';
  const toolbar=node('div'), editorRoot=node('div');root.append(toolbar,editorRoot);
  let presets=[], selectedPresets=new Map(), refreshPreset=()=>{};
  const draftNames=new Map();
  const editor=GoblinButtonEditor(editorRoot,()=>{save();refreshPreset();changed();},translate);
  const current=()=>bindings?.find(b=>b.id===selected);
  const save=()=>{if(current())current().profiles=editor.value();else if(bindings===null)controls.profiles=editor.value();};
  const hosts=()=>[{id:'mac',name:sound.mac_model||'Mac'},...(sound.channels?.devices||[]).map(d=>({id:'remote:'+d.id,name:(d.audio_devices?.model||d.name||'Phone / tablet')+' · '+d.id.slice(-4)}))];
  const peripherals=host=>host==='mac'?(sound.media_controls_devices||[]):(sound.channels?.devices||[]).find(d=>'remote:'+d.id===host)?.audio_devices?.buttons||[];
  const online=b=>hosts().some(h=>h.id===b.host)&&(b.device==='all'||peripherals(b.host).some(d=>d.id===b.device));
  function show(){
    editorRoot.hidden=bindings!==null&&!current();
    if(!editorRoot.hidden){editor.fill(current()||controls,lang);refreshPreset();}
    editor.active(current()?sound.media_controls_states?.[selected]?.mode||'':sound.media_controls_mode||'normal');
  }
  function addBinding(host,device,name,profiles){
    const b={id:'ctl_'+Date.now().toString(36)+'_'+Math.random().toString(36).slice(2,9),host,device,name,enabled:true,profiles:clone(profiles)};
    bindings.push(b);return b;
  }
  function migrate(){
    if(bindings!==null)return;
    save();bindings=[];
    const source=controls.source||'all';
    for(const host of hosts().filter(h=>source==='all'||source===h.id)){
      const device=host.id==='mac'?(controls.mac_device||'all'):(controls.remote_devices?.[host.id.slice(7)]||'all');
      const binding=addBinding(host.id,device,peripherals(host.id).find(d=>d.id===device)?.name||(device==='all'?('All device buttons'):device),controls.profiles);
      if(selectedPresets.has('')) selectedPresets.set(binding.id,selectedPresets.get(''));
    }
  }
  function presetControls(){
    const group=node('fieldset'), title=node('legend',('Button profiles'));
    const picker=node('select'), name=node('input'), saveButton=node('button');
    const deleteButton=node('button',('Delete profile'));
    const target=current()?.id||'';
    let optionNames;
    picker.setAttribute('aria-label',title.textContent);
    name.placeholder=('Profile name');
    name.setAttribute('aria-label',name.placeholder);
    name.maxLength=64;
    name.value=draftNames.get(target)||'';
    for(const button of [saveButton,deleteButton]){
      button.type='button';
      button.className='g-btn';
    }
    deleteButton.dataset.goblinId='voice:buttons:preset:delete';
    const namedPreset=()=>presets.find(p=>p.name===name.value) || presets.find(p=>p.name===name.value.trim());
    const updateButtons=()=>{
      const existing=presets.some(p=>p.name===name.value.trim());
      saveButton.textContent=existing?('Update profile'):('Save profile');
      saveButton.disabled=!name.value.trim();
      deleteButton.disabled=!namedPreset();
    };
    refreshPreset=()=>{
      const names=JSON.stringify(presets.map(p=>p.name));
      if(names!==optionNames){
        picker.replaceChildren(new Option(('Current assignments'),''));
        for(const p of presets)picker.add(new Option(p.name,p.name));
        optionNames=names;
      }
      const preferred=presets.find(p=>p.name===selectedPresets.get(target));
      const matches=presets.filter(p=>editor.matches(p.profiles));
      const match=matches.includes(preferred)?preferred:(!selectedPresets.has(target)&&matches.length===1?matches[0]:null);
      picker.value=match?.name||'';
      if(!draftNames.has(target)) name.value=match?.name||preferred?.name||'';
      updateButtons();
    };
    name.oninput=()=>{draftNames.set(target,name.value);updateButtons();};
    saveButton.onclick=()=>{
      const key=name.value.trim();
      if(!key)return;
      const existing=presets.find(p=>p.name===key);
      if(existing)existing.profiles=editor.value();
      else presets.push({name:key,profiles:editor.value()});
      selectedPresets.set(target,key);
      draftNames.delete(target);
      refreshPreset();changed();
    };
    deleteButton.onclick=()=>{
      const preset=namedPreset();
      if(!preset) return;
      presets=presets.filter(p=>p!==preset);
      for(const [id,selectedName] of selectedPresets){
        if(selectedName===preset.name) selectedPresets.set(id,'');
      }
      draftNames.delete(target);
      refreshPreset();changed();
    };
    picker.onchange=()=>{
      const preset=presets.find(p=>p.name===picker.value);
      if(!preset){refreshPreset();return;}
      (current()||controls).profiles=clone(preset.profiles);
      selectedPresets.set(target,preset.name);
      draftNames.delete(target);
      show();changed();
    };
    group.append(title,picker,name,saveButton,deleteButton);
    group.style.cssText='display:grid;gap:8px;margin:12px 0;padding:12px';
    return group;
  }
  function draw(){
    toolbar.replaceChildren();
    const label=node('label',('Controller: independent assignments')), select=node('select');select.style.cssText='display:block;width:100%;margin:10px 0';
    if(bindings===null)select.add(new Option(('Current shared assignments'),''));
    for(const b of bindings||[])select.add(new Option((hosts().find(h=>h.id===b.host)?.name||b.host)+' · '+b.name+(online(b)?'':(' · disconnected'))+(b.enabled?'':(' · disabled')),b.id));
    select.value=selected;select.onchange=()=>{save();selected=select.value;draw();show();};label.append(select);toolbar.append(label);
    const chosen=current();
    refreshPreset=()=>{};
    if(chosen||bindings===null)toolbar.append(presetControls());
    if(chosen){const row=node('label'),check=node('input');check.type='checkbox';check.checked=chosen.enabled;check.style.cssText='appearance:auto;width:18px;height:18px;margin-right:10px';
      check.onchange=()=>{chosen.enabled=check.checked;if(check.checked)for(const b of bindings)if(b.id!==chosen.id&&b.host===chosen.host&&(b.device==='all'||chosen.device==='all'))b.enabled=false;changed();draw();show();};row.append(check,node('span',('Enable this controller')));toolbar.append(row);}
    const buttons=node('div');buttons.style.cssText='display:flex;gap:10px;flex-wrap:wrap;margin:14px 0';
    for(const [text,reset] of [[('Clear all assignments'),true],[('Restore defaults'),false]]){
      const b=node('button',text);b.type='button';b.className='g-btn';b.disabled=(!chosen&&bindings!==null)||(!reset&&!defaults);
      b.onclick=()=>{save();const p=reset?Object.fromEntries(Object.entries(editor.value()).map(([m,keys])=>[m,Object.fromEntries(Object.keys(keys).map(k=>[k,k.endsWith('_gap_ms')?1000:[]]))])):clone(defaults);if(chosen)chosen.profiles=p;else controls.profiles=p;show();changed();};buttons.append(b);
    }toolbar.append(buttons);
    const more=node('details'),summary=node('summary',('Add another controller'));more.append(summary);
    const hostLabel=node('label',('Device')),hostSelect=node('select'),peripheralLabel=node('label',('Headset / peripheral')),deviceSelect=node('select'),add=node('button',('Add controller'));
    for(const h of hosts())hostSelect.add(new Option(h.name,h.id));
    for(const el of [hostSelect,deviceSelect])el.style.cssText='display:block;width:100%;margin:8px 0 16px';
    const refresh=()=>{deviceSelect.replaceChildren();for(const d of peripherals(hostSelect.value))if(!(bindings||[]).some(b=>b.host===hostSelect.value&&b.device===d.id))deviceSelect.add(new Option(d.name,d.id));add.disabled=!deviceSelect.options.length;};hostSelect.onchange=refresh;refresh();
    add.type='button';add.className='g-btn';add.onclick=()=>{
      save();const host=hostSelect.value,device=deviceSelect.value,name=deviceSelect.selectedOptions[0]?.textContent;
      if(!peripherals(host).some(d=>d.id===device))return;
      migrate();let b=bindings.find(b=>b.host===host&&b.device===device);
      if(!b)b=addBinding(host,device,name,defaults||controls.profiles);
      for(const other of bindings)if(other.host===host&&other.id!==b.id&&other.device==='all')other.enabled=false;
      b.enabled=true;selected=b.id;draw();show();changed();
    };
    hostLabel.append(hostSelect);peripheralLabel.append(deviceSelect);more.append(hostLabel,peripheralLabel,add,node('p',('Only connected devices are listed. Added controllers operate simultaneously with independent modes. “All device buttons” is disabled when individual controllers are selected.')));toolbar.append(more);
    const legacy=document.getElementById('media_controls.source')?.closest('.row');if(legacy)legacy.hidden=bindings!==null;
    const macRow=document.getElementById('mediaMacDeviceRow');if(macRow&&bindings!==null)macRow.hidden=true;
  }
  return {
    fill(value,language,savedPresets,savedSelection){
      presets=clone(savedPresets||[]);
      selectedPresets=new Map(Object.entries(savedSelection||{}));
      controls=clone(value||{});lang=language;bindings=Array.isArray(controls.bindings)?clone(controls.bindings):null;
      if(!bindings?.some(b=>b.id===selected))selected=bindings?.[0]?.id||'';
      draw();show();
    },
    presets:()=>clone(presets),
    presetSelection:()=>Object.fromEntries(selectedPresets),
    value(){save();return bindings===null?{profiles:clone(controls.profiles)}:{bindings:clone(bindings)};},
    devices(s){sound=s;defaults=s.media_controls_defaults||defaults;const stamp=JSON.stringify([hosts(),hosts().map(h=>peripherals(h.id)),defaults]);if(stamp!==inventoryStamp){inventoryStamp=stamp;draw();refreshPreset();}editor.active(current()?s.media_controls_states?.[selected]?.mode||'':s.media_controls_mode||'normal');},
  };
};
