"""Hosted-browser proof of concept (2026-09-15).

Question being tested: can WE host the browser (like ChatGPT Agent does), let the
student log in *inside* it from a normal web page, keep that login on disk, and
let an agent reuse the very same browser later?

Three moving parts, all in this file so the mechanism is visible:

  1. Persistent browser   Playwright launch_persistent_context(user_data_dir=…)
                          -> cookies / Duo "trusted device" survive restarts.
  2. Live view (hand-off) CDP Page.startScreencast -> JPEG frames over WebSocket
                          -> <canvas> in the user's browser. Mouse/keyboard go back
                          through Playwright page.mouse / page.keyboard.
  3. Agent access         Same context, no second login: GET /agent/read returns
                          what the page says, GET /status probes Canvas' API.

Run:
    python hosted_browser.py --user demo                 # fresh profile
    python hosted_browser.py --user demo --import-state ~/.ainl/canvas.wisc.edu.storage_state.json
Then open http://localhost:8765/ and log in there. Stop with Ctrl+C.

Not production: no auth on the viewer URL, one user per process, no TLS.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from http import HTTPStatus
from pathlib import Path

from playwright.async_api import async_playwright
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

VIEW_W, VIEW_H = 1200, 850

HTML = """<!doctype html><meta charset=utf-8><title>hosted browser</title>
<style>body{margin:0;background:#111;color:#ddd;font:13px system-ui}
#bar{padding:6px 10px;display:flex;gap:8px;align-items:center}
#url{flex:1;padding:4px 8px;background:#222;color:#eee;border:1px solid #444}
canvas{display:block;outline:none;cursor:default}</style>
<div id=bar><span id=st>connecting…</span><input id=url placeholder="https://…"><button id=go>go</button>
<button id=snap>save session</button></div>
<canvas id=c width=%(w)d height=%(h)d></canvas>
<textarea id=kb style="position:fixed;left:-9999px;top:0;opacity:0" autocomplete=off></textarea>
<script>
const c=document.getElementById('c'),x=c.getContext('2d'),st=document.getElementById('st');
const ws=new WebSocket((location.protocol==='https:'?'wss':'ws')+'://'+location.host+'/ws');
const send=o=>ws.readyState===1&&ws.send(JSON.stringify(o));
ws.onmessage=e=>{const m=JSON.parse(e.data);
  if(m.type==='frame'){const im=new Image();im.onload=()=>x.drawImage(im,0,0,c.width,c.height);im.src='data:image/jpeg;base64,'+m.data;}
  if(m.type==='url'){st.textContent=m.url;}};
ws.onopen=()=>st.textContent='connected';ws.onclose=()=>st.textContent='disconnected';
const pos=e=>{const r=c.getBoundingClientRect();return{x:Math.round((e.clientX-r.left)*c.width/r.width),y:Math.round((e.clientY-r.top)*c.height/r.height)}};
const kb=document.getElementById('kb');
kb.oninput=e=>{if(e.inputType==='insertText'&&e.data){send({type:'text',text:e.data});}kb.value='';};
kb.onkeydown=e=>{if(e.key.length>1||e.ctrlKey||e.metaKey||e.altKey){send({type:'key',key:e.key,ctrl:e.ctrlKey,meta:e.metaKey,alt:e.altKey});e.preventDefault();}};
c.onmousedown=e=>{kb.focus();send({type:'mouse',kind:'down',...pos(e),button:e.button});e.preventDefault()};
c.onmouseup=e=>send({type:'mouse',kind:'up',...pos(e),button:e.button});
c.onmousemove=e=>{if(e.buttons)send({type:'mouse',kind:'move',...pos(e)})};
c.onwheel=e=>{send({type:'wheel',...pos(e),dx:e.deltaX,dy:e.deltaY});e.preventDefault()};
c.oncontextmenu=e=>e.preventDefault();
document.getElementById('go').onclick=()=>send({type:'nav',url:document.getElementById('url').value});
document.getElementById('url').onkeydown=e=>{if(e.key==='Enter')document.getElementById('go').click()};
document.getElementById('snap').onclick=()=>fetch('/snapshot').then(r=>r.text()).then(t=>alert(t));
</script>"""

# keys whose event.key name Playwright understands verbatim
SPECIAL = {"Enter", "Backspace", "Tab", "Escape", "Delete", "ArrowUp", "ArrowDown", "ArrowLeft",
           "ArrowRight", "Home", "End", "PageUp", "PageDown", "Shift", "Control", "Alt", "Meta", " "}


class HostedBrowser:
    def __init__(self, user: str, root: Path, base_url: str):
        self.user, self.base_url = user, base_url.rstrip("/")
        self.dir = root / user
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ctx = self.page = self.cdp = None
        self.clients: set = set()

    async def start(self, pw, import_state: Path | None):
        self.ctx = await pw.chromium.launch_persistent_context(
            str(self.dir / "profile"), headless=True, args=["--no-proxy-server"],
            viewport={"width": VIEW_W, "height": VIEW_H})
        own = self.dir / "storage_state.json"
        if not import_state and own.exists():
            import_state = own  # reseed from our last snapshot (session cookies do NOT survive in the profile dir)
        if import_state:  # one-off migration from the desktop-login flow
            state = json.loads(import_state.read_text(encoding="utf-8"))
            await self.ctx.add_cookies(state.get("cookies", []))
            print(f"imported {len(state.get('cookies', []))} cookies from {import_state.name}")
        self.page = self.ctx.pages[0] if self.ctx.pages else await self.ctx.new_page()
        self.page.on("framenavigated", lambda f: f == self.page.main_frame and self._broadcast({"type": "url", "url": f.url}))
        self.cdp = await self.ctx.new_cdp_session(self.page)
        self.cdp.on("Page.screencastFrame", self._on_frame)
        await self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 55, "maxWidth": VIEW_W, "maxHeight": VIEW_H, "everyNthFrame": 1})
        await self.page.goto(self.base_url + "/", wait_until="commit")

    # -- live view ------------------------------------------------------------
    def _on_frame(self, ev):
        asyncio.ensure_future(self.cdp.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]}))
        self._broadcast({"type": "frame", "data": ev["data"]})

    def _broadcast(self, msg):
        raw = json.dumps(msg)
        for ws in list(self.clients):
            asyncio.ensure_future(ws.send(raw))

    async def handle_input(self, m: dict):
        t = m.get("type")
        if t == "mouse":
            btn = {0: "left", 1: "middle", 2: "right"}.get(m.get("button", 0), "left")
            await self.page.mouse.move(m["x"], m["y"])
            if m["kind"] == "down":
                await self.page.mouse.down(button=btn)
            elif m["kind"] == "up":
                await self.page.mouse.up(button=btn)
        elif t == "wheel":
            await self.page.mouse.move(m["x"], m["y"])
            await self.page.mouse.wheel(m["dx"], m["dy"])
        elif t == "key":
            k = m["key"]
            mods = [n for n, on in (("Control", m.get("ctrl")), ("Meta", m.get("meta")), ("Alt", m.get("alt"))) if on]
            if k in SPECIAL or mods:
                await self.page.keyboard.press("+".join(mods + ["Space" if k == " " else k]))
            elif len(k) == 1:
                await self.page.keyboard.type(k)
        elif t == "text":
            await self.page.keyboard.type(m["text"])
        elif t == "nav" and m.get("url"):
            await self.page.goto(m["url"], wait_until="commit")

    # -- agent side -----------------------------------------------------------
    async def status(self) -> dict:
        try:
            code = await self.page.evaluate("async () => (await fetch('/api/v1/users/self',{credentials:'include'})).status")
        except Exception as e:  # not on Canvas yet, or navigating
            code = f"n/a ({type(e).__name__})"
        cookies = await self.ctx.cookies()
        meta = sorted({(c["name"], c.get("expires", -1) in (-1, None)) for c in cookies if "canvas" in c["domain"]})
        active = await self.page.evaluate("() => {const a=document.activeElement; return a ? {tag:a.tagName, id:a.id, value:(a.value||'').slice(0,40)} : null}")
        return {"user": self.user, "url": self.page.url, "canvas_api_status": code, "logged_in": code == 200,
                "canvas_cookies(name, is_session_cookie)": meta, "active_element": active}

    async def read(self) -> dict:
        text = await self.page.evaluate("() => document.body ? document.body.innerText.slice(0, 1500) : ''")
        return {"url": self.page.url, "title": await self.page.title(), "text": text}

    async def close_and_exit(self):
        await self.ctx.close()
        import os; os._exit(0)

    async def snapshot(self) -> str:
        out = self.dir / "storage_state.json"
        await self.ctx.storage_state(path=str(out))
        return f"session saved to {out}"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="demo")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--base-url", default="https://canvas.wisc.edu")
    ap.add_argument("--root", default=str(Path.home() / ".ainl" / "hosted"))
    ap.add_argument("--import-state", type=Path)
    a = ap.parse_args()

    hb = HostedBrowser(a.user, Path(a.root), a.base_url)

    async def http(conn, req):  # plain HTTP for everything except /ws
        path = req.path.split("?")[0]
        if path == "/ws":
            return None
        body, ctype = None, "text/html; charset=utf-8"
        if path == "/":
            body = HTML % {"w": VIEW_W, "h": VIEW_H}
        elif path == "/status":
            body, ctype = json.dumps(await hb.status()), "application/json"
        elif path == "/agent/read":
            body, ctype = json.dumps(await hb.read(), ensure_ascii=False), "application/json"
        elif path == "/snapshot":
            body, ctype = await hb.snapshot(), "text/plain"
        elif path == "/shutdown":  # graceful: snapshot, close browser, exit
            body, ctype = await hb.snapshot(), "text/plain"
            asyncio.get_event_loop().call_later(0.5, lambda: asyncio.ensure_future(hb.close_and_exit()))
        if body is None:
            return Response(HTTPStatus.NOT_FOUND, "Not Found", Headers(), b"nope")
        return Response(HTTPStatus.OK, "OK", Headers([("Content-Type", ctype)]), body.encode("utf-8"))

    async def ws_handler(ws):
        hb.clients.add(ws)
        try:
            await ws.send(json.dumps({"type": "url", "url": hb.page.url}))
            async for raw in ws:
                try:
                    await hb.handle_input(json.loads(raw))
                except Exception as e:
                    print("input error:", type(e).__name__, str(e)[:100])
        finally:
            hb.clients.discard(ws)

    async with async_playwright() as pw:
        await hb.start(pw, a.import_state)
        async with serve(ws_handler, "127.0.0.1", a.port, process_request=http, max_size=8 * 2**20):
            print(f"hosted browser for '{a.user}' -> http://localhost:{a.port}/   (profile: {hb.dir / 'profile'})", flush=True)
            await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
