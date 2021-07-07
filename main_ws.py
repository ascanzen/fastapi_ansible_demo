from fastapi import FastAPI
from starlette.endpoints import WebSocketEndpoint, HTTPEndpoint
from starlette.responses import HTMLResponse
from starlette.routing import Route, WebSocketRoute
import subprocess

html = """
<!DOCTYPE html>
<html>
    <head>
        <title>Chat</title>
    </head>
    <body>
        <h1>WebSocket Chat</h1>
        <form action="" onsubmit="sendMessage(event)">
            <input type="text" id="messageText" autocomplete="off" placeholder="" />
            <button>Send</button>
        </form>
        <ul id='messages'>
        </ul>
        <script>
            document.getElementById("messageText").placeholder="第一次输入内容为昵称";

            var ws = new WebSocket("ws://localhost:8000/ws");

            // 接收
            ws.onmessage = function(event) {
                // 获取id为messages的ul标签内
                var messages = document.getElementById('messages')
                // 创建li标签
                var message = document.createElement('li')
                // 创建内容
                var content = document.createTextNode(event.data)
                // 内容添加到li标签内
                message.appendChild(content)
                // li标签添加到ul标签内
                messages.appendChild(message)
            };

            var name = 0;
            // 发送
            function sendMessage(event) {
                var input = document.getElementById("messageText")
                ws.send(input.value)
                input.value = ''
                event.preventDefault()

                if (name == 0){
                    document.getElementById("messageText").placeholder="";
                    name = 1;
                }
            }
        </script>
    </body>
</html>
"""

info = {}


class Homepage(HTTPEndpoint):
    async def get(self, request):
        return HTMLResponse(html)


class Echo(WebSocketEndpoint):
    encoding = "text"

    # 修改socket
    async def alter_socket(self, websocket):
        socket_str = str(websocket)[1:-1]
        socket_list = socket_str.split(' ')
        socket_only = socket_list[3]
        return socket_only

    # 连接 存储
    async def on_connect(self, websocket):
        await websocket.accept()

        # 用户输入名称
        name = await websocket.receive_text()

        socket_only = await self.alter_socket(websocket)
        # 添加连接池 保存用户名

        info[socket_only] = [name, websocket]

        # 先循环 告诉之前的用户有新用户加入了R
        for wbs in info:
            print(wbs)
            print(info[socket_only][0])
            await info[wbs][1].send_text(f"{info[socket_only][0]}-加入了聊天室")
        #
        print(info)

    # 收发
    async def on_receive(self, websocket, data):
        socket_only = await self.alter_socket(websocket)
        if data == 'run nginx':
            command = 'df -Ph'
            task = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out = task.stdout.readlines()
            for wbs in info:
                print(info[socket_only], data)
                await info[wbs][1].send_text(f"{out}")

        for wbs in info:
            print(info[socket_only], data)
            await info[wbs][1].send_text(f"{info[socket_only][0]}: {data}")

    # 断开 删除
    async def on_disconnect(self, websocket, close_code):
        socket_only = await self.alter_socket(websocket)
        # 删除连接池
        pp = info.pop(socket_only)
        print(pp)
        pass


routes = [
    Route("/", Homepage),
    WebSocketRoute("/ws", Echo)
]

app = FastAPI(routes=routes)

if __name__ == '__main__':
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0")
