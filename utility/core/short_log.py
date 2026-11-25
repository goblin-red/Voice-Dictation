"""Presentation-only reduction of duplicate dictation rows; full log stays intact."""
import ast
import json
import re
from datetime import datetime

STAMP=re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:,\d+)?) \w+ (.*)$')
SOURCE=re.compile(r' \[zipa-source:(.*)\]$')
AUDIO=re.compile(r' \[audio:[0-9.]+:[0-9.]+\]$')

def parse(line):
    m=STAMP.match(line)
    if not m:return None
    stamp=m[1];msg=re.sub(r'^\[[^\]]*\] ', '', m[2])
    at=datetime.strptime(stamp,'%Y-%m-%d %H:%M:%S,%f' if ',' in stamp else '%Y-%m-%d %H:%M:%S').timestamp()
    return at,msg

def quoted(msg):
    msg=AUDIO.sub('',msg)
    if ' → ' in msg:msg=msg.rsplit(' → ',1)[0]
    try:return ast.literal_eval(msg)
    except (ValueError,SyntaxError):return None

def clean(lines):
    parsed=[parse(line) for line in lines]
    heard=[];backups=[];typed=[]
    for i,p in enumerate(parsed):
        if not p:continue
        at,msg=p
        h=re.match(r'^(?:услышано(?: \(ввод\))?|heard(?: \(typing\))?): (.*)',msg)
        t=re.match(r'^(?:напечатано|typed) (.*)',msg)
        b=re.match(r'^(?:fallback|страховка) [\w-]+ (?:in|за) [0-9.]+ (?:s|с): (.*)',msg)
        if h:
            value=quoted(h[1])
            if isinstance(value,str):heard.append((i,at,value))
        if t:
            value=quoted(t[1])
            if isinstance(value,str):typed.append((i,at,value))
        if b:backups.append((i,at,msg))
    hide=set()
    for i,at,msg in backups:
        src=SOURCE.search(msg)
        if src:
            try:source=json.loads(src[1])
            except ValueError:continue
        else:
            # Historical logs: suppress only when exactly one distinct phrase is plausible.
            previous=max((t for _,t,_ in backups if t<at),default=at-5)
            choices={text for _,t,text in heard if max(previous,at-5)<=t<=at}
            if len(choices)!=1:continue
            source=choices.pop()
        matches=[(j,t,text) for j,t,text in heard if 0<=at-t<=15 and text==source]
        if not matches:continue
        latest=max(t for _,t,_ in matches)
        hide.update(j for j,t,_ in matches if abs(t-latest)<2)
        # Printed IPA may omit the final spoken command. Don't hide unrelated text.
        def norm(s):return ' '.join(s.split()).rstrip(' .,')
        for j,t,text in typed:
            if latest<=t<=at+3 and norm(text) and (norm(source)==norm(text) or norm(source).startswith(norm(text)+' ')):
                hide.add(j)
    # Remaining duplicate heard rows: preserve one, regardless of backup availability.
    seen=[]
    for i,at,text in heard:
        if i in hide:continue
        if any(text==v and abs(at-t)<2 for t,v in seen):hide.add(i)
        else:seen.append((at,text))
    for j,t,text in typed:
        normalized=' '.join(text.split()).rstrip(' .,')
        if any(0<=t-at<=5 and normalized and
               (source.rstrip(' .,')==normalized or source.startswith(normalized+' '))
               for _,at,source in heard):
            hide.add(j)
    out=[]
    for i,line in enumerate(lines):
        if i in hide:continue
        p=parsed[i]
        if p and p[1].startswith(('Audio diagnostics:', 'аудиодиагностика:')):continue
        out.append(AUDIO.sub('',SOURCE.sub('',line)))
    return out
