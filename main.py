from fastapi import FastAPI
from starlette.endpoints import WebSocketEndpoint, HTTPEndpoint
from starlette.responses import HTMLResponse, FileResponse
import subprocess
from fastapi import FastAPI, WebSocket
from util.ansible_api import BaseInventory, AnsibleAPI, CallbackModule
from ansible.plugins.callback import CallbackBase

app = FastAPI()


@app.get("/")
async def get():
    return FileResponse("static/index.html")


@app.websocket("/ws")
async def websocket_endpoint(
        websocket: WebSocket
):
    await websocket.accept()
    while True:
        data = await websocket.receive_json()
        hostdat = [{
            'hostname': data.get("hostname", "test"),
            'ip': data.get("host", "test"),
            'port': data.get("port", 22),
            'password': data.get("pass", 22),
            'groups': ['gituwen', data.get("hostname", "test")],
        }, ]
        inventory = BaseInventory(hostdat)
        ansible_api = AnsibleAPI(
            dynamic_inventory=inventory,
            callback=CallbackModule(),
        )
        ansible_api.run_module(module_name="shell", module_args=data.get("cmd", "ls -lha /tmp"),hosts=data.get("hostname", "test"))
        req = ansible_api.get_result()
        await websocket.send_json(req)

if __name__ == '__main__':
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0")
