"""Tail only a verified participant's Codex transcript, starting at channel delivery."""
import hashlib
import json
import re
from pathlib import Path

APPROVAL = re.compile(r"(?:yes|approved|i approve|continue|go ahead|да|разрешаю|подтверждаю|продолжай|даю разрешение)(?:[.! ]*)", re.I)
RECEIPT = re.compile(r"^\[gbus (t[\w-]+h\d+)(?: [^\]]+)?\]")


def asks_approval(text):
    return "?" in text and bool(re.search(r"permission|allow|approve|confirm|разреш|подтвер|одобр", text, re.I))


def signature(text):
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()


def attach(channel, key, path, *, offset=None):
    if not path or channel[key].get("agent") not in ("codex","claude"):
        return
    watches = channel.setdefault("watches", {})
    if key in watches:
        return
    try:
        st = Path(path).stat()
    except OSError:
        return
    watches[key] = dict(agent=channel[key]["agent"], path=str(path), inode=st.st_ino, offset=st.st_size if offset is None else offset,
                        expected={}, turns={}, awaiting=False, outputs={})


def claude_event(w, event):
    """Only follow an explicit user envelope via Claude parent UUIDs."""
    if event.get("isSidechain") or event.get("isMeta"):
        return
    kind = event.get("type")
    uid, parent = event.get("uuid"), event.get("parentUuid")
    if not uid or kind not in ("user","assistant"):
        return
    msg = event.get("message") or {}
    blocks = msg.get("content",[])
    text = blocks if isinstance(blocks,str) else "\n".join(
        b["text"] for b in blocks if b.get("type")=="text" and b.get("text"))
    chain = w.setdefault("claude_chain",{})
    if kind=="user" and not (isinstance(blocks,list) and any(b.get("type")=="tool_result" for b in blocks)):
        tid = uid
        chain[uid] = tid
    elif parent in chain:
        tid = chain[parent]
        chain[uid] = tid
    else:
        return
    if text:
        return dict(type="response_item",payload=dict(type="message",role=kind,
                    content=[dict(type="input_text" if kind=="user" else "output_text",text=text)],
                    phase="commentary",turn_id=tid))


def consume(w, event, unique):
    if w.get("agent") == "claude":
        event = claude_event(w,event) or {}
    p = event.get("payload") or {}
    meta = event.get("metadata") or {}
    retained = meta.get("retained_source") or {}
    tid = (p.get("turn_id") or (retained.get("id") or {}).get("turn_id")
           or (p.get("internal_chat_message_metadata_passthrough") or {}).get("turn_id"))
    if not tid:
        return
    if event.get("type") == "event_msg":
        if p.get("type") in ("task_complete","turn_aborted","task_aborted","task_failed"):
            turn = w["turns"].pop(tid,None)
            if turn and p["type"] == "task_complete" and p.get("last_agent_message"):
                return tid, p["last_agent_message"]
        return
    if event.get("type") != "response_item":
        return
    if p.get("type") == "function_call" and tid in w["turns"]:
        if "request_user_input" in p.get("name",""):
            w["awaiting"] = True
        return
    if p.get("type") != "message":
        return
    role = p.get("role")
    if role == "user":
        context = p.get("internal_chat_message_metadata_passthrough") or {}
        kinds = context.get("content_item_kinds") or []
        if meta.get("client_authored") is False and kinds and all(
            k in ("agents_md.instructions","environments.environment_context") for k in kinds):
            return
        text = " ".join(v.get("text","") for v in p.get("content",[]) if v.get("type") in ("input_text","text")).strip()
        receipt = RECEIPT.match(text)
        own = receipt and w["expected"].get(receipt[1]) == signature(text)
        if own:
            del w["expected"][receipt[1]]
        continuation = (not receipt and unique and w["awaiting"] and APPROVAL.fullmatch(text))
        if tid in w["turns"] and w["turns"][tid] != text:
            # The same approval can arrive as a steered user input in the existing turn.
            if continuation:
                w["awaiting"] = False
                return
            w["turns"].pop(tid,None)
            own = continuation = False
        if own or continuation:
            w["turns"][tid] = text
            w["current_turn"] = tid
        else:
            w.pop("current_turn",None)
        w["awaiting"] = False
    elif role == "assistant" and tid in w["turns"] and p.get("phase") in ("commentary","final_answer","final"):
        if retained.get("complete") is False:
            return
        text = "\n".join(v["text"] for v in p.get("content",[]) if v.get("type") == "output_text" and v.get("text")).strip()
        if text:
            if asks_approval(text):
                w["awaiting"] = True
            return tid, text


def poll(w, unique, deliver):
    path = Path(w["path"])
    try:
        st = path.stat()
        if st.st_ino != w["inode"] or st.st_size < w["offset"]:
            w["invalid"] = True
        if w.get("invalid") or st.st_size == w["offset"]:
            return False
        with path.open("rb") as stream:
            stream.seek(w["offset"])
            data = stream.read(4*1024*1024)
            if b"\n" not in data and len(data)==4*1024*1024:
                data += stream.readline()
    except OSError:
        return False
    end = data.rfind(b"\n")
    if end < 0:
        return False
    for line in data[:end+1].splitlines(keepends=True):
        try:
            event = json.loads(line)
        except (ValueError,UnicodeError):
            w["offset"] += len(line)
            continue
        result = addressed_event(w, event) if w.get("addressed") else consume(w,event,unique)
        w["offset"] += len(line)
        if result:
            tid,text = result
            token = tid + ":" + signature(text)
            if token not in w["outputs"]:
                w["outputs"][token] = True
                deliver(tid,text)
    return True

ADDRESS = re.compile(r"^(?:Синьер,\s*)?\[(to|dialog|кому|диалог):\s*([^\[\]\n]+?)\s*\]\s*", re.I)
NOTICE = "[Voice · recipient]"


def destination(text, routes):
    """Return exactly one active pair and its body, or request clarification."""
    match = ADDRESS.match(text)
    if not match:
        return None, ""
    kind, value = match.groups()
    body = text[match.end():].strip()
    if not body or ADDRESS.match(body):
        return None, ""
    matches = []
    for channel, peer in routes:
        terminal = peer["tty"].removeprefix("/dev/").removeprefix("ttys")
        if kind.casefold() in ("dialog", "диалог"):
            hit = value == channel
        else:
            hit = value.casefold() in (peer["nick"].casefold(), terminal, "ttys" + terminal)
        if hit:
            matches.append(channel)
    return (matches[0], body) if len(matches) == 1 else (None, "")


INBOUND = re.compile(r"^\[(?:от:[^\]\n]+|Вы из [^\]\n]+|Агент [^\]\n]+|Voice · старт|from:[^\]\n]+|You from [^\]\n]+|Agent [^\]\n]+|Voice · start)\]\[(?:dialog|диалог):(t[\w-]+)\]")


RELAY = re.compile(r"^\[Voice · (?:relay|передача) (t[\w-]+h\d+)\]")
POLICY = "[Voice · instructions]"


def remember_input(w, text, turn):
    receipts = w.get("voice_inputs", {})
    delivered = receipts.get(text, 0)
    if delivered == 1:
        del receipts[text]
    elif delivered:
        receipts[text] -= 1
    queues = w.get("input_audiences", {})
    queue = queues.get(text)
    audience = queue.pop(0) if queue else None
    if queue == []:
        del queues[text]
    w["reply_to_terminal_user"] = audience == "remote_agent"
    route = INBOUND.match(text) if delivered else None
    w["input_channel"] = route[1] if route else None
    w["input_turn"] = turn
    w["notice_input"] = bool(delivered and text.startswith((NOTICE, "[Voice · адресат]")))
    w["voice_reply_allowed"] = bool((route or w["notice_input"]) and audience != "terminal_user")
    w["allow_local_address"] = not delivered
    w.pop("direct_input", None)
    match = ADDRESS.match(text)
    if not delivered and match:
        w["direct_input"] = [match[1].casefold(), match[2].strip().casefold(), text[match.end():].strip()]
        return "addressed-user:" + str(w["offset"]), text
    relay = RELAY.match(text) if delivered else None
    if relay:
        return "relay:" + relay[1], text


def addressed_event(w, event):
    """Read visible assistant messages only. Terminal input and tool results never route."""
    if w["agent"] == "claude":
        if event.get("isSidechain") or event.get("isMeta"):
            return
        p = event.get("message") or {}
        if event.get("type") == "user":
            content = p.get("content", "")
            if isinstance(content, str):
                return remember_input(w, content, None)
            elif isinstance(content, list) and content and all(b.get("type") == "text" for b in content):
                return remember_input(w, "\n".join(b["text"] for b in content), None)
        if event.get("type") != "assistant" or p.get("role") != "assistant":
            return
        blocks = p.get("content", [])
        w["reply_channel"] = w.get("input_channel")
        token = event.get("uuid")
    else:
        p = event.get("payload") or {}
        retained = (event.get("metadata") or {}).get("retained_source") or {}
        turn = (retained.get("id") or {}).get("turn_id") or (p.get("internal_chat_message_metadata_passthrough") or {}).get("turn_id") or p.get("turn_id")
        if event.get("type") == "response_item" and p.get("role") == "user":
            inputs = [b.get("text","") for b in p.get("content",[]) if b.get("type") == "input_text"]
            kinds = (p.get("internal_chat_message_metadata_passthrough") or {}).get("content_item_kinds", [])
            if kinds and len(kinds) == len(p.get("content", [])):
                inputs = [b["text"] for k,b in zip(kinds,p["content"]) if k == "user.text" and b.get("type") == "input_text"]
            elif kinds and any(k != "user.text" for k in kinds):
                return
            if inputs:
                return remember_input(w, "\n".join(inputs), turn)
        if event.get("type") != "response_item" or p.get("type") != "message" or p.get("role") != "assistant":
            return
        retained = (event.get("metadata") or {}).get("retained_source") or {}
        if retained.get("complete") is False or p.get("phase") not in ("commentary", "final", "final_answer"):
            return
        if turn != w.get("input_turn"):
            return
        w["reply_channel"] = w.get("input_channel") if turn == w.get("input_turn") else None
        blocks = p.get("content", [])
        token = (retained.get("id") or {}).get("message_id") or p.get("id")
    text = "\n".join(b["text"] for b in blocks if b.get("type") in ("text", "output_text") and b.get("text")).strip()
    if not w.get("voice_reply_allowed"):
        match = ADDRESS.match(text)
        if not w.get("allow_local_address") or not match:
            return
        if w.get("direct_input") == [match[1].casefold(), match[2].strip().casefold(), text[match.end():].strip()]:
            return
    if text and (not w.get("notice_input") or ADDRESS.match(text)):
        return str(token or w["offset"]), text
