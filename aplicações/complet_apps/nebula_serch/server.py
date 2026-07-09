import asyncio
import websockets
import json
from flask import Flask, render_template
from playwright.async_api import async_playwright
import threading

app = Flask(__name__)

# -------------------------
# Navegação com Playwright
# -------------------------
async def handle_browser(link: str) -> dict:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)  # headless=False para ver
        page = await browser.new_page()
        await page.goto(link, timeout=60000)

        title = await page.title()
        content = await page.content()

        await browser.close()

        return {
            "title": title,
            "length": len(content),
            "url": link
        }

async def ws_handler(websocket):
    async for message in websocket:
        try:
            data = json.loads(message)
            link = data.get("url")

            if not link or not link.startswith("http"):
                response = {"error": "URL inválida"}
            else:
                response = await handle_browser(link)

        except Exception as e:
            response = {"error": str(e)}

        await websocket.send(json.dumps(response, ensure_ascii=False))

async def ws_server():
    async with websockets.serve(ws_handler, "localhost", 8765):
        print("🚀 Servidor WebSocket rodando em ws://localhost:8765")
        await asyncio.Future()  # Mantém rodando

def start_ws():
    asyncio.run(ws_server())

# -------------------------
# Flask (frontend)
# -------------------------
@app.route("/")
def index():
    return render_template("index.html")

if __name__ == "__main__":
    # roda o servidor websocket em outra thread
    threading.Thread(target=start_ws, daemon=True).start()

    # roda o flask
    app.run(debug=True, port=5500)
