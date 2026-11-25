/* Shared device-routing view. Data and actions come from the host application. */
(function () {
  window.GoblinDevices = {
    model(sound, language, actual = '') {
      const s=sound||{}, c=s.channels||{}, en=language==='en';
      if(s.runtime)actual=['mic','speech'].map(k=>s.runtime[k]?.text||'').filter(Boolean).join(' · ');
      const devices=[{id:'mac',name:s.mac_model||'Mac',detail:[s.mac_in,s.mac_out,...(s.media_controls_devices||[]).map(d=>('Buttons: ' )+d.name)].filter(Boolean).join(' · ')},
        ...(c.devices||[]).map(d=>({id:'remote:'+d.id,name:(d.audio_devices?.model||d.name||('Phone / tablet' ))+' · '+d.id.slice(-4),detail:['Connected to Remote' ,...new Set([d.audio_devices?.input_headset,d.audio_devices?.output_headset].filter(Boolean))].join(' · ')}))];
      const route=(role,ch)=>s[role]==='phone'?'remote:'+(c[ch]||'unavailable'):s[role]||'off';
      const inventory=role=>(c.devices||[]).find(d=>d.id===c[role])?.audio_devices;
      const outInventory=inventory('output'),inInventory=inventory('input');
      const mac=s.speech==='mac', output=mac?s.audio_out:s.phone_output;
      const outputs=mac?[{id:'auto',name:'System default' },...(s.mac_outputs||[]).map(name=>({id:name,name}))]:[{id:'auto',name:'Automatic' },{id:'speaker',name:'Speaker' },...(outInventory?.output_headset?[{id:'headset',name:outInventory.output_headset}]:[])];
      const micMac=s.mic==='mac', input=micMac?s.audio_in:s.phone_input;
      const inputs=micMac?[{id:'auto',name:'System default' },...(s.mac_inputs||[]).map(name=>({id:name,name}))]:[{id:'auto',name:'Automatic' },{id:'phone',name:'Device microphone' },...(inInventory?.input_headset?[{id:'headset',name:inInventory.input_headset}]:[])];
      const source=s.media_controls?.source||'all';
      const bindings=Array.isArray(s.media_controls?.bindings)?s.media_controls.bindings:null;
      const buttonDevices=source==='mac'?(s.media_controls_devices||[]):((c.devices||[]).find(d=>'remote:'+d.id===source)?.audio_devices?.buttons||[]);
      return {language,devices,outputs,inputs,buttonDevices,bindings,buttonsEnabled:!!s.media_controls?.enabled,actual,disabled:!sound,selections:{button_device:source==='mac'?(s.media_controls?.mac_device||'all'):(s.media_controls?.remote_devices?.[source.slice(7)]||'all'),input:input?.mode==='manual'?input.name:'auto',output:output?.mode==='manual'?output.name:'auto',mic:route('mic','input'),speech:route('speech','output'),video:c.video?'remote:'+c.video:'off',buttons:bindings?'configured':s.media_controls?.enabled?(s.media_controls.source||'all'):'off'}};
    },
    mount(root, change) {
      const style = document.createElement('style');
      style.textContent = `.gd{color:inherit}.gd-head{display:flex;justify-content:space-between;gap:16px;align-items:center}.gd-note{opacity:.7;font-size:13px;line-height:1.5}.gd-routes,.gd-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,240px),1fr));gap:12px;margin:18px 0}.gd-route,.gd-card{border:1px solid #8884;border-radius:14px;padding:16px;min-width:0;background:#88808}.gd-route label{display:block;font-weight:600;margin-bottom:8px}.gd-route select{box-sizing:border-box;width:100%;max-width:100%;padding:9px;border-radius:8px;background:transparent;color:inherit;border:1px solid #8887;font:inherit}.gd-route option{color:#111;background:#fff}.gd-card h3{margin:0 0 5px;overflow-wrap:anywhere}.gd-tags{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}.gd-tag{font-size:12px;border-radius:20px;padding:5px 9px;background:#638cff20;border:1px solid #638cff55}.gd-status{min-height:22px;white-space:pre-wrap}.gd-status[data-error=true]{color:#e77777}.gd-online{color:#55b485;font-size:12px;overflow-wrap:anywhere}.gd-head>.gd-online{text-align:right;max-width:65%}.gd-empty{padding:14px;border:1px dashed #8886;border-radius:10px}.gd-card small{display:block;overflow-wrap:anywhere}`;
      root.append(style);
      const body = document.createElement('div'); body.className = 'gd'; root.append(body);
      let model = null, pending = null, timer = null, signature = '';
      const titles = () => ({mic:('Dictation'),speech:('Speech playback'),buttons:('Control buttons'),video:('Mac screen')});
      const hints = () => ({mic:('Device → recognition on Mac'),speech:('Voice responses → device'),buttons:('Whose buttons run commands'),video:('Mac screen → viewing device')});
      const node = (tag,text,cls) => { const n=document.createElement(tag); if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n; };
      const status = (text,error=false) => {const n=body.querySelector('.gd-status');if(n){n.textContent=text;n.dataset.error=String(error);}};
      async function assign(role,target) {
        pending={role,target};updateDisabled();status(('Waiting for assignment confirmation…'));
        clearTimeout(timer);timer=setTimeout(()=>{pending=null;updateDisabled();status(('Assignment not confirmed; check the device connection.'),true);},18000);
        try{await change(role,target);}catch(e){clearTimeout(timer);pending=null;updateDisabled();status(e.message||String(e),true);}
      }
      function draw() {
        const heading=node('div',undefined,'gd-head');
        heading.append(node('h2',('Devices')),node('span',`${model.devices.length} · ${('connected')}: ${model.devices.map(d=>d.name).join(', ')}`,'gd-online'));
        const intro=node('p',('Shared with the browser and Voice settings: changes here also appear there.'),'gd-note');
        const routes=node('div',undefined,'gd-routes');
        for(const role of ['mic','speech','buttons']) {
          const box=node('div',undefined,'gd-route'); const label=node('label',titles()[role]);
          if(role==='buttons'&&model.bindings){
            box.append(label,node('p',model.buttonsEnabled?('Independent controllers'):('Controls disabled')));
            for(const b of model.bindings){
              const host=model.devices.find(d=>d.id===b.host);
              box.append(node('small',(host?.name||b.host)+' · '+b.name+' · '+(b.enabled&&model.buttonsEnabled?('enabled'):('disabled')),'gd-note'));
            }
            box.append(node('p',('Choose controllers, modes and commands in Voice → Headset buttons.'),'gd-note'));
            routes.append(box);continue;
          }
          const select=node('select');select.setAttribute('aria-label',titles()[role]);select.dataset.role=role;
          const opts=[{id:'off',name:('Off')}];
          if(role==='buttons') opts.push({id:'all',name:('All connected')});
          opts.push(...model.devices.filter(d=>role!=='video'||d.id!=='mac'));
          const selected=model.selections[role]||'off';
          const missingDevice=!opts.some(d=>d.id===selected);
          if(missingDevice)box.append(node('small',('Selected device is disconnected'),'gd-note'));
          for(const d of opts) select.add(new Option(d.name,d.id));
          select.value=selected; select.disabled=!!pending||model.disabled||model.disabledRoles?.includes(role);
          select.addEventListener('change',()=>assign(role,select.value));
          label.append(select);box.append(label,node('small',hints()[role],'gd-note'));
          if((role==='speech'||role==='mic')&&selected!=='off'&&!missingDevice) {
            const input=role==='mic', subrole=input?'input':'output';
            const title=input?('Microphone input'):('Playback output');
            const outLabel=node('label',title);outLabel.style.marginTop='14px';
            const out=node('select');out.dataset.role=subrole;out.setAttribute('aria-label',title);
            const opts=[...((input?model.inputs:model.outputs)||[])], value=model.selections[subrole];
            if(!opts.some(d=>d.id===value))box.append(node('small',('Selected device is disconnected or the app has not reported it yet'),'gd-note'));
            for(const d of opts)out.add(new Option(d.name,d.id));out.value=value;
            out.addEventListener('change',()=>assign(subrole,out.value));outLabel.append(out);box.append(outLabel);
          }
          if(role==='buttons'&&selected!=='off'&&selected!=='all'&&!missingDevice) {
            const deviceLabel=node('label',('Peripheral'));deviceLabel.style.marginTop='14px';
            const sub=node('select');sub.dataset.role='button_device';sub.setAttribute('aria-label',('Button peripheral'));
            sub.add(new Option(('All connected peripherals'),'all'));
            for(const d of model.buttonDevices||[])sub.add(new Option(d.name,d.id));
            const value=model.selections.button_device;sub.value=value;
            if(value!=='all'&&!(model.buttonDevices||[]).some(d=>d.id===value))box.append(node('small',('Selected peripheral is disconnected'),'gd-note'));
            if(!(model.buttonDevices||[]).length)box.append(node('small',('No connected button peripherals reported yet'),'gd-note'));
            sub.addEventListener('change',()=>assign('button_device',sub.value));deviceLabel.append(sub);box.append(deviceLabel);
          }
          routes.append(box);
        }
        const feedback=node('p','', 'gd-status');feedback.setAttribute('role','status');feedback.setAttribute('aria-live','polite');
        const actual=node('p',model.actual||'', 'gd-note');
        const cards=node('div',undefined,'gd-cards');
        for(const d of model.devices) {
          const card=node('article',undefined,'gd-card');card.append(node('h3',d.name),node('span',('Connected'),'gd-online'));
          if(d.detail)card.append(node('small',d.detail,'gd-note'));
          const tags=node('div',undefined,'gd-tags');
          for(const role of ['mic','speech','buttons']) if(model.selections[role]===d.id||(role==='buttons'&&(model.selections.buttons==='all'||model.buttonsEnabled&&model.bindings?.some(b=>b.enabled&&b.host===d.id)))) tags.append(node('span',titles()[role],'gd-tag'));
          if(!tags.childNodes.length) tags.append(node('small',('No assignments'),'gd-note'));
          card.append(tags);cards.append(card);
        }
        const note=node('p',('An assigned channel does not mean recording or playback has started. Android buttons require the updated app; iPad Safari does not forward volume buttons.'),'gd-note');
        body.replaceChildren(heading,intro,routes,feedback,actual,cards,note);
        if(model.devices.length===1)cards.after(node('p',('Phones and tablets appear here after connecting to Remote.'),'gd-empty'));
      }
      function updateDisabled(){body.querySelectorAll('select').forEach(s=>{s.disabled=!!pending||model.disabled||model.disabledRoles?.includes(s.dataset.role==='output'?'speech':s.dataset.role==='input'?'mic':s.dataset.role==='button_device'?'buttons':s.dataset.role);if(!pending)s.value=model.selections[s.dataset.role]||'off';});}
      return {update(next){
        model=next;
        const confirmed=!!pending&&model.selections[pending.role]===pending.target;
        if(pending&&model.selections[pending.role]===pending.target){pending=null;clearTimeout(timer);signature='';status(('Assignment confirmed'));}
        const nextSignature=JSON.stringify(next);
        const topology=JSON.stringify([next.devices,next.inputs,next.outputs,next.buttonDevices]);
        const topologyChanged=topology!==root.dataset.topology;
        root.dataset.topology=topology;
        if(nextSignature!==signature&&(confirmed||topologyChanged||!body.contains(document.activeElement))&&!pending){signature=nextSignature;draw();}
        updateDisabled();
      }};
    }
  };
})();
